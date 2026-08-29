#!/usr/bin/env python3
"""
fl_hook.py -- First Light pre-edit advisory hook.

Bob calls this before a file write.  It reads a JSON payload from stdin,
resolves which function unit the target line range falls into, and prints
one advisory line to stdout.  It ALWAYS exits 0.  It never blocks an edit.

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
# 1. FIRST_LIGHT_EVIDENCE env var (absolute path) — set this in the hook
#    configuration when evidence.json is not adjacent to the target files.
# 2. Walk up from the target file's directory until evidence.json is found.
# This order lets the hook work both when evidence lives at the repo root of
# the analysed project and when it lives somewhere else entirely.


# ---------------------------------------------------------------------------
# Constants — must stay in sync with first_light.py
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


def _is_file_stale(abs_path: str, integrity: dict) -> bool:
    """Return True if the file's current hash differs from the recorded hash."""
    recorded = integrity.get(abs_path, "")
    if not recorded:
        return True   # not in the integrity table — treat as stale
    current = _sha256(abs_path)
    return current != recorded


def _overlaps(unit: dict, start_line: int | None, end_line: int | None) -> bool:
    """Return True if [start_line, end_line] overlaps the unit's body range."""
    if start_line is None:
        return True   # no line info → conservatively match any unit in the file
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
    baseline: dict  = ev.get("baseline", {})

    # ── stale check on the specific file ─────────────────────────────────
    file_stale = _is_file_stale(abs_path, integrity)

    # ── find matching units ───────────────────────────────────────────────
    # Units are keyed by  "<abs_file>::<qualified_name>"
    matching = [
        (key, u)
        for key, u in units.items()
        if u.get("file") == abs_path and _overlaps(u, start_line, end_line)
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
                f"{ev_path.name} (recorded against: {baseline.get('package', '?')})"
            )
        if file_stale:
            msg += " [FILE CHANGED since observation]"
        return msg

    # ── build advisory ────────────────────────────────────────────────────
    # If multiple units match, report the most specific (smallest body).
    matching.sort(key=lambda kv: kv[1]["body_end"] - kv[1]["body_start"])
    key, unit = matching[0]
    qualname   = key.split("::", 1)[1] if "::" in key else key
    provenance = unit.get("provenance", "unknown")
    label      = _PROVENANCE_LABELS.get(provenance, provenance)

    runner = Path(baseline.get("runner", "?")).name

    if provenance == PROVENANCE_NEVER:
        explanation = (
            f"{qualname} has never been observed executing "
            f"(runner: {runner})"
        )
    elif provenance == PROVENANCE_IN_SITU:
        explanation = (
            f"{qualname} was observed executing under normal operation "
            f"(runner: {runner})"
        )
    elif provenance == PROVENANCE_DRIVER:
        explanation = (
            f"{qualname} only ran because a driver was built to reach it "
            f"(runner: {runner})"
        )
    else:
        explanation = f"{qualname} -- provenance: {provenance}"

    msg = f"[first_light] {label} -- {explanation}"
    if file_stale:
        msg += " [FILE CHANGED since observation -- re-run first_light.py]"

    return msg


# ---------------------------------------------------------------------------
# Main entry point (hook mode)
# ---------------------------------------------------------------------------

def _parse_stdin() -> tuple[str, int | None]:
    """Read stdin, parse JSON, return (file_path, line_number) via recursive scan."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    return _extract_fields(payload)


def _hook_main() -> None:
    file_path, start_line = _parse_stdin()

    if not file_path:
        _print("[first_light] no path in payload -- nothing to check")
        sys.exit(0)

    try:
        result = query(file_path, start_line, None)
    except Exception as exc:
        result = f"[first_light] internal error ({exc}) -- advisory skipped"

    _print(result)
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
        file_path, start_line = _parse_stdin()
        if not file_path:
            return "[first_light] no path in payload -- nothing to check"
        return query(file_path, start_line, None)
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
