#!/usr/bin/env python3
"""
fl_hook.py -- First Light pre-edit advisory hook.

Bob calls this before a file write.  It reads a JSON payload from stdin,
resolves which function unit the target line range falls into, and prints
one advisory line to stdout.  It exits 0 and never blocks an edit, unless
--strict is passed (or FIRST_LIGHT_STRICT=1), in which case an edit to a
function with no execution record exits 2.  Off by default.

Stdin payload -- Bob may nest tool_input at any depth, e.g.:
    {"tool_input": {"file_path": "...", "line": 7}}
    {"path": "...", "start_line": 30, "end_line": 45}
    {"file": "...", "line_number": 12}

The script walks the entire JSON tree recursively.  It takes the first
value that looks like a file path from any key named: file_path, filePath,
path, or file.  It takes the first integer from any key named: line,
line_number, start_line, or offset.  If a "range" object is present, its
start is used as the line number.  All other fields are ignored.

Stdout (pure ASCII):
    [first_light] <provenance> -- <one-line explanation>

Evidence file location:
    1. FIRST_LIGHT_EVIDENCE env var (absolute path to evidence.json).
    2. Walk up the directory tree from the target file.

Stale detection:
    An observation is stale when the SHA-256 of a source file no longer
    matches the hash recorded in evidence.json at generation time.
    "stale" is a derived property computed on read; it is NOT stored in
    evidence.json itself.

Usage:
    echo '{"path":"/some/file.py","start_line":30}' | python fl_hook.py
    python fl_hook.py --selftest [--evidence /path/to/evidence.json]
"""

import hashlib
import io
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Evidence file resolution order
# ---------------------------------------------------------------------------
# 1. FIRST_LIGHT_EVIDENCE env var (absolute path) -- set this in the hook
#    configuration when evidence.json is not adjacent to the target files.
# 2. Walk up from the target file's directory until evidence.json is found.
# This order lets the hook work both when evidence lives at the repo root of
# the analysed project and when it lives somewhere else entirely.


# ---------------------------------------------------------------------------
# Constants -- must stay in sync with first_light.py
# ---------------------------------------------------------------------------
EVIDENCE_FILENAME   = "evidence.json"
PROVENANCE_NEVER    = "never_observed"
PROVENANCE_IN_SITU  = "observed_in_situ"
PROVENANCE_DRIVER   = "observed_under_driver"

_PROVENANCE_LABELS = {
    PROVENANCE_NEVER:   "never observed",
    PROVENANCE_IN_SITU: "observed in situ",
    PROVENANCE_DRIVER:  "observed under driver",
}

# Pure-ASCII stdout wrapper -- prevents UnicodeEncodeError on Windows consoles
# (e.g. cp1252 / cp850) and ensures no non-ASCII bytes ever reach the output.
_stdout = io.TextIOWrapper(
    sys.stdout.buffer,
    encoding="ascii",
    errors="replace",
    line_buffering=True,
)


def _print(msg: str) -> None:
    """Write *msg* + newline to the ASCII-safe stdout wrapper."""
    _stdout.write(msg + "\n")
    _stdout.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _find_evidence(start: Path) -> Path | None:
    """Locate evidence.json.

    Checks, in order:
    1. FIRST_LIGHT_EVIDENCE environment variable (absolute path to the file).
    2. Walk up the directory tree from *start* until the file is found.
    """
    env_path = os.environ.get("FIRST_LIGHT_EVIDENCE", "")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p

    candidate = start if start.is_dir() else start.parent
    while True:
        ev = candidate / EVIDENCE_FILENAME
        if ev.is_file():
            return ev
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def _load_evidence(ev_path: Path) -> dict | None:
    try:
        with open(ev_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _file_integrity(abs_path: str, integrity: dict) -> str:
    """Return the file's integrity state against the recorded evidence.

    Three states, deliberately not collapsed into a boolean:

      "unrecorded" -- the file is not in the integrity table.  Nothing was ever
                     recorded about it, so nothing can be said to have changed.
      "changed"    -- a hash was recorded and the file no longer matches it.
      "unchanged"  -- the file still matches the hash recorded with the evidence.

    Reporting "unrecorded" as if it were "changed" would assert a fact that was
    never observed, which is the failure this whole tool exists to refuse.
    """
    recorded = integrity.get(abs_path, "")
    if not recorded:
        # Same portability problem as unit lookup: the integrity table is keyed
        # by the producing machine's absolute paths.
        for k, v in integrity.items():
            if _same_file(k, abs_path):
                recorded = v
                break
    if not recorded:
        return "unrecorded"
    return "unchanged" if _sha256(abs_path) == recorded else "changed"


# Provenance ordered by how little is known. An edit that crosses several units
# is as unobserved as its least observed part, and "least" has three levels, not
# two: the middle one is the distinction this whole project rests on, so
# collapsing it here would undo the argument in the one place it gets used.
_SEVERITY = {PROVENANCE_NEVER: 0, PROVENANCE_DRIVER: 1, PROVENANCE_IN_SITU: 2}


def _worst(units) -> str | None:
    """Provenance of the least observed unit in *units*."""
    known = [u.get("provenance") for u in units if u.get("provenance") in _SEVERITY]
    return min(known, key=lambda p: _SEVERITY[p]) if known else None


def _overlaps(unit: dict, start_line: int | None, end_line: int | None) -> bool:
    """Return True if [start_line, end_line] overlaps the unit's body range.

    With no line number every unit in the file matches.  That is not an
    assertion that the edit touches any of them; it is the caller's cue that the
    edit could not be located, and the caller must say so rather than pick one.
    """
    if start_line is None:
        return True
    s = start_line
    e = end_line if end_line is not None else start_line
    return s <= unit["body_end"] and e >= unit["body_start"]


# ---------------------------------------------------------------------------
# Payload extraction -- recursive field search
# ---------------------------------------------------------------------------

# Keys recognised as carrying a file path, in priority order.
_PATH_KEYS  = {"file_path", "filepath", "path", "file"}
# Keys recognised as carrying a line number, in priority order.
_LINE_KEYS  = {"line", "line_number", "start_line", "offset"}


def _extract_fields(obj: object) -> tuple[str, int | None]:
    """Recursively walk *obj* and return (file_path, line_number).

    Uses a breadth-first scan so shallower values win over deeper ones when
    the same key name appears at multiple levels.
    """
    file_path:  str | None = None
    line_number: int | None = None

    queue = [obj]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                k = key.lower().replace("-", "_")

                # -- file path candidate --
                if file_path is None and k in _PATH_KEYS:
                    if isinstance(value, str) and value.strip():
                        file_path = value.strip()

                # -- line number candidate --
                if line_number is None and k in _LINE_KEYS:
                    try:
                        line_number = int(value)
                    except (TypeError, ValueError):
                        pass

                # -- range object: use its start as line number --
                if line_number is None and k == "range" and isinstance(value, dict):
                    for rk in ("start", "start_line", "line", "begin"):
                        if rk in value:
                            try:
                                line_number = int(value[rk])
                                break
                            except (TypeError, ValueError):
                                pass

                # enqueue nested structures
                if isinstance(value, (dict, list)):
                    queue.append(value)

        elif isinstance(node, list):
            queue.extend(node)

    return (file_path or ""), line_number


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------

def _same_file(recorded: str, edited: str) -> bool:
    """Return True when *recorded* and *edited* are the same source file.

    evidence.json keys every unit by the absolute path of the machine that
    produced it. Comparing those strings to the path being edited means the hook
    matches nothing on any other clone, and --strict then fails open: it finds no
    unit, decides it knows nothing, and lets every edit through. A gate that is
    silently disabled everywhere except the author's machine is worse than no
    gate, because it reports as if it were working.

    Compare the absolute paths first, and fall back to the longest shared tail of
    the two paths. Two files that agree on package, module and filename are the
    same file for this purpose, and disagreeing on the directory above the
    package is exactly what a different checkout looks like.
    """
    if not recorded:
        return False
    a = Path(recorded).parts
    b = Path(edited).parts
    if Path(recorded) == Path(edited):
        return True
    # at least the file and its two enclosing directories must agree
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n].lower() == b[-1 - n].lower():
        n += 1
    return n >= 3


def provenance_for(
    file_path: str,
    start_line: int | None,
    end_line: int | None = None,
) -> str | None:
    """Return the provenance of the unit an edit lands in, or None.

    None means the question could not be answered: the edit could not be placed
    inside a single function, the file is not tracked, or the evidence is
    missing. A caller deciding whether to block must treat None as "unknown",
    never as "unobserved". Reading the answer out of the advisory string instead
    was the bug this exists to remove: the not-located message ends with
    "N never observed", and a substring test on it blocked every edit to any
    file holding more than one function.
    """
    abs_path = str(Path(file_path).resolve())
    ev_path = _find_evidence(Path(abs_path))
    if ev_path is None:
        return None
    ev = _load_evidence(ev_path)
    if not ev:
        return None
    units: dict = ev.get("units", {})
    matching = [
        u for _k, u in units.items()
        if _same_file(u.get("file", ""), abs_path) and _overlaps(u, start_line, end_line)
    ]
    if not matching:
        return None
    if start_line is None and len(matching) > 1:
        return None          # the edit was not placed; nothing is known about it
    # An edit crossing several functions is as unobserved as its least observed
    # part. Reporting the smallest body would let a rewrite of never-observed
    # code through because it happened to also touch an observed neighbour, and
    # reporting only the never_observed case would do the same to a function
    # that ran solely under a driver.
    if len(matching) > 1:
        return _worst(matching)
    matching.sort(key=lambda u: u["body_end"] - u["body_start"])
    return matching[0].get("provenance")


def query(
    file_path: str,
    start_line: int | None,
    end_line: int | None,
) -> str:
    """Return the advisory string for an edit to *file_path* at *start_line*."""
    abs_path = str(Path(file_path).resolve())

    # ── locate evidence.json ─────────────────────────────────────────────
    ev_path = _find_evidence(Path(abs_path))
    if ev_path is None:
        return (
            "[first_light] no evidence -- evidence.json not found in any "
            "ancestor directory; observation status unknown"
        )

    ev = _load_evidence(ev_path)
    if ev is None:
        return (
            f"[first_light] no evidence -- {ev_path} exists but could not be "
            "parsed; observation status unknown"
        )

    integrity: dict = ev.get("integrity", {})
    units: dict     = ev.get("units", {})
    baselines: list = ev.get("baselines", [])

    # ── stale check on the specific file ─────────────────────────────────
    file_state = _file_integrity(abs_path, integrity)

    # ── find matching units ───────────────────────────────────────────────
    # Units are keyed by  "<abs_file>::<qualified_name>"
    matching = [
        (key, u)
        for key, u in units.items()
        if _same_file(u.get("file", ""), abs_path) and _overlaps(u, start_line, end_line)
    ]

    if not matching:
        # File is known (in integrity) but no unit at this range.
        if abs_path in integrity:
            loc = f"line {start_line}" if start_line else "this location"
            msg = (
                f"[first_light] no unit at {loc} in "
                f"{Path(abs_path).name} -- not inside any tracked function body"
            )
        else:
            msg = (
                f"[first_light] {Path(abs_path).name} is not covered by "
                f"{ev_path.name} (recorded against: "
                f"{Path(baselines[0]['package']).name if baselines else '?'})"
            )
        if file_state == "changed":
            msg += " [FILE CHANGED since observation]"
        return msg

    # ── build advisory ────────────────────────────────────────────────────
    # If multiple units match, report the most specific (smallest body).
    # Bob's Edit, Write and MultiEdit payloads carry no line number, and those
    # are the matchers this hook is wired to.  Without one, every unit in the
    # file matched, and naming the smallest would assert something the payload
    # does not support: precisely the failure this project exists to detect.
    # Report the file's state instead, and say the edit could not be placed.
    if start_line is None and len(matching) > 1:
        never = sum(1 for _, u in matching
                    if u.get("provenance") == PROVENANCE_NEVER)
        msg = (
            f"[first_light] edit not located within {Path(abs_path).name} "
            f"(no line number in payload) -- {len(matching)} tracked functions "
            f"here, {never} never observed"
        )
        if file_state == "changed":
            msg += " [FILE CHANGED since observation -- re-run first_light.py]"
        return msg

    # An edit can be precisely located and still cross more than one function.
    # Naming the smallest would report one provenance for a change that carries
    # several, and it fails in the direction that reassures: an edit spanning an
    # observed function and a never-observed one would read as observed. Name
    # every unit the edit touches instead, worst provenance first.
    if len(matching) > 1:
        ranked = sorted(
            matching,
            key=lambda kv: (_SEVERITY.get(kv[1].get("provenance"), 3),
                            kv[1]["body_start"]),
        )
        never = sum(1 for _k, u in ranked if u.get("provenance") == PROVENANCE_NEVER)
        driver = sum(1 for _k, u in ranked if u.get("provenance") == PROVENANCE_DRIVER)
        counts = f"{never} never observed"
        if driver:
            counts += f", {driver} observed only under a driver"
        names = [
            (k.split("::", 1)[1] if "::" in k else k).rsplit("#", 1)[0]
            for k, _u in ranked[:3]
        ]
        more = f" and {len(ranked) - 3} more" if len(ranked) > 3 else ""
        msg = (
            f"[first_light] this edit spans {len(ranked)} tracked functions -- "
            f"{counts}. Least observed first: {', '.join(names)}{more}"
        )
        if file_state == "changed":
            msg += " [FILE CHANGED since observation -- re-run first_light.py]"
        return msg

    matching.sort(key=lambda kv: kv[1]["body_end"] - kv[1]["body_start"])
    key, unit = matching[0]
    _qname_raw = key.split("::", 1)[1] if "::" in key else key
    qualname   = _qname_raw.rsplit("#", 1)[0] if "#" in _qname_raw else _qname_raw
    provenance = unit.get("provenance", "unknown")
    label      = _PROVENANCE_LABELS.get(provenance, provenance)

    # Name the baselines that actually observed this unit.  With more than one
    # baseline a single "runner" is a conflation: a function reached by the test
    # suite but never by the running program is not the same fact as one reached
    # by both, and the advisory should not flatten them.
    _all_ids  = [b.get("id", "?") for b in baselines]
    _seen_ids = unit.get("observed_in_baseline") or []
    seen  = ", ".join(_seen_ids) if _seen_ids else "none"
    scope = ", ".join(_all_ids) if _all_ids else "?"

    if provenance == PROVENANCE_NEVER:
        explanation = (
            f"{qualname} has never been observed executing "
            f"(baselines run: {scope})"
        )
        # A driver may have reached this function and had its promotion refused.
        # Saying only "never observed" would drop that, and the difference is
        # the whole point: no baseline ran it, something did run it, and the
        # claim about where it runs in production could not be verified.
        if unit.get("driver_reached"):
            _n = len(unit.get("driver_reached_lines") or [])
            _why = ("no call site was declared"
                    if not unit.get("driver_declared_call_site")
                    else "its declared call site could not be confirmed")
            explanation += (
                f". A driver did reach it, executing {_n} line"
                f"{'' if _n == 1 else 's'} of the body, but {_why}, "
                f"so the promotion was refused"
            )
    elif provenance == PROVENANCE_IN_SITU:
        explanation = (
            f"{qualname} was observed executing under normal operation "
            f"(observed by: {seen})"
        )
    elif provenance == PROVENANCE_DRIVER:
        explanation = (
            f"{qualname} only ran because a driver was built to reach it "
            f"(not reached by: {scope})"
        )
    else:
        explanation = f"{qualname} -- provenance: {provenance}"

    msg = f"[first_light] {label} -- {explanation}"
    if file_state == "changed":
        msg += " [FILE CHANGED since observation -- re-run first_light.py]"

    return msg


# ---------------------------------------------------------------------------
# Main entry point (hook mode)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Locating the edit when the payload carries no line number
# ---------------------------------------------------------------------------
#
# This is the common case, not an edge case.  The hook is wired to Edit, Write
# and MultiEdit, and none of the three sends a line number, so before this the
# advisory fell through to "edit not located" on essentially every real edit:
# the one thing the hook exists to do never ran in normal use.
#
# But those tools do send the text.  Edit and MultiEdit send `old_string`,
# which is present verbatim in the file on disk, and Write sends the full new
# `content`, which can be diffed against what is there now.  Either one places
# the edit precisely enough to name the function.
#
# The rule when the text is ambiguous is to refuse rather than guess.  An
# `old_string` that appears twice in the file does not identify a location, and
# reporting the first hit would assert something the payload does not support.
# That is the same failure this project exists to detect, so the hook declines
# it in itself.

_ANCHOR_KEYS = ("old_string", "content")


def _extract_anchors(obj: object) -> dict:
    """Collect edited-text anchors from the payload, keyed by field name."""
    found: dict = {k: [] for k in _ANCHOR_KEYS}
    queue = [obj]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                k = key.lower().replace("-", "_")
                if k in found and isinstance(value, str) and value:
                    found[k].append(value)
                if isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(node, list):
            queue.extend(node)
    return found


def _line_at(text: str, offset: int) -> int:
    """1-based line number containing *offset*."""
    return text.count("\n", 0, offset) + 1


def _span_of_substring(text: str, needle: str) -> tuple[int, int] | None:
    """Line span of *needle* in *text*, or None if absent or not unique."""
    if not needle:
        return None
    first = text.find(needle)
    if first < 0:
        return None
    if text.find(needle, first + 1) != -1:
        return None
    return _line_at(text, first), _line_at(text, first + len(needle) - 1)


def _span_of_rewrite(old_text: str, new_text: str) -> tuple[int, int] | None:
    """Span of the changed region in the OLD file for a whole-file write."""
    import difflib

    a = old_text.splitlines()
    b = new_text.splitlines()
    lo = hi = None
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        start, end = i1 + 1, max(i2, i1 + 1)
        lo = start if lo is None else min(lo, start)
        hi = end if hi is None else max(hi, end)
    return (lo, hi) if lo is not None else None


def locate_edit(file_path: str, payload: object) -> tuple[int, int] | None:
    """Resolve the edited line span from the payload's text, or None.

    Returns the union of every anchor that resolved, so a MultiEdit touching
    two functions reports a span covering both rather than silently picking one.
    """
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    anchors = _extract_anchors(payload)
    olds = anchors["old_string"]
    if olds:
        spans = [_span_of_substring(text, needle) for needle in olds]
        # A MultiEdit whose anchors do not all resolve is not partially known,
        # it is unknown: reporting the edits that did resolve would describe a
        # different edit from the one being made.
        if any(sp is None for sp in spans):
            return None
    else:
        spans = [sp for content in anchors["content"]
                 if (sp := _span_of_rewrite(text, content))]
    if not spans:
        return None
    return min(s for s, _ in spans), max(e for _, e in spans)


def resolve_payload(payload: object) -> tuple[str, int | None, int | None, bool]:
    """Return (file_path, start_line, end_line, located_by_text) for a payload.

    Shared by the hook and its self-test so the test exercises the path that
    actually runs.  The previous self-test reimplemented this inline, which is
    why it reported a clean run while the only interesting branch was dead.
    """
    file_path, start_line = _extract_fields(payload)
    if not file_path or start_line is not None:
        return file_path, start_line, None, False
    span = locate_edit(file_path, payload)
    if span:
        return file_path, span[0], span[1], True
    return file_path, None, None, False


def _parse_stdin() -> tuple[str, int | None, object]:
    """Read stdin and return (file_path, line_number, payload).

    The payload comes back too because the line number is usually absent and
    the edited text in the payload is what locates the edit instead.
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    fp, ln = _extract_fields(payload)
    return fp, ln, payload


def _hook_main() -> None:
    # --strict turns the advisory into a gate. It is off by default and stays
    # off by default: a tool that blocks an edit the first time you install it
    # gets disabled the same afternoon, and a disabled hook reports nothing.
    # But an advisory that can never say no is a report, not a control, and an
    # org that wants the control should not have to fork the file to get it.
    strict = "--strict" in sys.argv or os.environ.get("FIRST_LIGHT_STRICT") == "1"

    _fp, _ln, payload = _parse_stdin()
    file_path, start_line, end_line, located_by_text = resolve_payload(payload)

    if not file_path:
        _print("[first_light] no path in payload -- nothing to check")
        sys.exit(0)

    try:
        result = query(file_path, start_line, end_line)
        if located_by_text:
            result += " [located from the edited text]"
    except Exception as exc:
        # An internal failure must not block work. The tool refusing to assert
        # what it cannot verify applies to itself: it does not know whether this
        # edit is safe, so it does not claim it is unsafe either.
        _print(f"[first_light] internal error ({exc}) -- advisory skipped")
        sys.exit(0)

    _print(result)

    if strict:
        prov = provenance_for(file_path, start_line, end_line)
        if prov == PROVENANCE_NEVER:
            _print(
                "[first_light] blocked by --strict: this edit reaches code "
                "with no execution record. Run it, add a driver that declares "
                "a verifiable call site, or edit with --strict off and accept "
                "that nothing observed it."
            )
            sys.exit(2)
        # prov is None when the edit could not be placed in one function. Not
        # knowing which function is being edited is not evidence that it is
        # unobserved, and blocking on it would stop every edit to any file with
        # more than one function in it.

    sys.exit(0)


# ---------------------------------------------------------------------------
# --selftest: runs against a known entry in evidence.json
# ---------------------------------------------------------------------------

def _run_hook_with_stdin(json_str: str) -> str:
    """Feed *json_str* through the real _parse_stdin + query path and return the output."""
    import io as _io
    old_stdin = sys.stdin
    try:
        sys.stdin = _io.StringIO(json_str)
        _fp, _ln, payload = _parse_stdin()
        file_path, start_line, end_line, located = resolve_payload(payload)
        if not file_path:
            return "[first_light] no path in payload -- nothing to check"
        out = query(file_path, start_line, end_line)
        return out + " [located from the edited text]" if located else out
    except Exception as exc:
        return f"[first_light] internal error ({exc}) -- advisory skipped"
    finally:
        sys.stdin = old_stdin


def _selftest() -> None:
    """Exercise the hook against real units and three payload shapes.

    Usage:
        python fl_hook.py --selftest
        python fl_hook.py --selftest --evidence /path/to/evidence.json
    """
    # Allow explicit path via --evidence <path> on the command line.
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--evidence" and i + 1 < len(args):
            os.environ["FIRST_LIGHT_EVIDENCE"] = args[i + 1]
            break
        if a.startswith("--evidence="):
            os.environ["FIRST_LIGHT_EVIDENCE"] = a.split("=", 1)[1]
            break

    ev_path = _find_evidence(Path(__file__).resolve())
    if ev_path is None:
        _print(
            "SELFTEST: evidence.json not found.\n"
            "  Pass --evidence <path> or set FIRST_LIGHT_EVIDENCE=<path>,\n"
            "  or run: python first_light.py --evidence <dest>"
        )
        sys.exit(1)

    # Pin the env var so that query() uses the same evidence file that _selftest()
    # found here, even when querying source files deep inside target/ whose directory
    # tree would otherwise cause _find_evidence to resolve a different (possibly
    # stale or absent) file.
    os.environ["FIRST_LIGHT_EVIDENCE"] = str(ev_path)

    ev = _load_evidence(ev_path)
    if ev is None:
        _print(f"SELFTEST: could not parse {ev_path}")
        sys.exit(1)

    units = ev.get("units", {})
    if not units:
        _print("SELFTEST: evidence.json contains no units")
        sys.exit(1)

    # Pick one observed_in_situ unit and one never_observed unit.
    examples: dict[str, tuple[str, dict]] = {}
    for prov in (PROVENANCE_IN_SITU, PROVENANCE_NEVER):
        for key, u in units.items():
            if u.get("provenance") == prov and prov not in examples:
                examples[prov] = (key, u)

    if not examples:
        _print("SELFTEST: no suitable units found")
        sys.exit(1)

    all_ok = True
    _print(f"SELFTEST using: {ev_path}\n")

    # ── Part 1: internal query() path ────────────────────────────────────
    _print("--- Part 1: internal query() path ---")
    for prov, (key, unit) in examples.items():
        fpath      = unit["file"]
        body_start = unit["body_start"]
        result     = query(fpath, body_start, body_start)
        expected   = _PROVENANCE_LABELS[prov]
        ok         = expected in result
        status     = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        _print(f"  [{status}] prov={prov}")
        _print(f"         file: {Path(fpath).name}:{body_start}")
        _print(f"         got:  {result}")
    _print("")

    # ── Part 2: three payload shapes through the real stdin path ─────────
    _print("--- Part 2: stdin payload shapes ---")
    # Use the first example unit for all three shapes.
    prov0, (key0, unit0) = next(iter(examples.items()))
    fpath0 = unit0["file"]
    line0  = unit0["body_start"]
    expected0 = _PROVENANCE_LABELS[prov0]

    # Escape backslashes for JSON (Windows paths)
    fpath0_j = fpath0.replace("\\", "\\\\")

    payload_shapes = [
        # Shape A: flat, keys "path" + "start_line"
        (
            "flat {path, start_line}",
            f'{{"path": "{fpath0_j}", "start_line": {line0}}}',
        ),
        # Shape B: flat, keys "file_path" + "line"
        (
            "flat {file_path, line}",
            f'{{"file_path": "{fpath0_j}", "line": {line0}}}',
        ),
        # Shape C: nested under tool_input, keys "file_path" + "line"
        (
            "nested {tool_input: {file_path, line}}",
            f'{{"tool_input": {{"file_path": "{fpath0_j}", "line": {line0}}}}}',
        ),
    ]

    for label, json_str in payload_shapes:
        result = _run_hook_with_stdin(json_str)
        ok     = expected0 in result
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        _print(f"  [{status}] shape: {label}")
        _print(f"         got:  {result}")
    _print("")

    # ── Part 3: graceful degradation ─────────────────────────────────────
    _print("--- Part 3: graceful degradation ---")
    degrade_cases = [
        # No path field at all.
        ("empty payload {}",             "{}",                        "no path"),
        # Path present but not in the integrity table: "not covered" or "no evidence".
        ("unknown file path",
         '{"path": "/no/such/dir/fake.py"}',  "covered"),
        # Completely unparseable input.
        ("malformed JSON",               "not json at all",           "no path"),
    ]

    # Payload shapes Bob actually sends.  Edit, Write and MultiEdit carry no
    # line number, and those are the matchers this hook is wired to, so the
    # case below is the common one rather than an edge case.
    if examples:
        _p0 = next(iter(examples.values()))[1]["file"].replace("\\", "\\\\")
        degrade_cases.append((
            "known file, edit with no line number",
            f'{{"tool_input": {{"file_path": "{_p0}", "old_string": "x"}}}}',
            "edit not located",
        ))
    # A function whose driver was refused must still report that the driver
    # reached it; saying only "never observed" would drop a recorded fact.
    _refused = [(k, u) for k, u in units.items()
                if u.get("driver_reached")
                and u.get("provenance") == PROVENANCE_NEVER]
    if _refused:
        _k, _u = _refused[0]
        _pf = _u["file"].replace("\\", "\\\\")
        degrade_cases.append((
            "never observed, but a refused driver reached it",
            f'{{"file_path": "{_pf}", "line": {_u["body_start"]}}}',
            "A driver did reach it",
        ))
    for label, json_str, expected_fragment in degrade_cases:
        result = _run_hook_with_stdin(json_str)
        ok     = expected_fragment in result
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        _print(f"  [{status}] case: {label}")
        _print(f"         got:  {result}")
    _print("")

    sys.exit(0 if all_ok else 1)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _hook_main()
