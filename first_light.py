#!/usr/bin/env python3
"""
First Light — which functions have ever been observed executing?

Usage:
    python first_light.py --package <pkg_path> --runner <runner.py>
                          [--python <interpreter>]
                          [--exclude <dir> ...]
                          [--rcfile <coverage_rc>]

Correctness rule
----------------
A function is counted as *observed* only when lines inside its **body** were
executed — i.e. lines from node.body[0].lineno onward.  The `def` line itself
always executes at import time, so starting the range at node.lineno would
falsely mark every imported function as observed and inflate the result toward
100 %.  We therefore start at node.body[0].lineno.
"""

from __future__ import annotations

import argparse
import ast
import copy
import datetime
import enum
import hashlib
import json
import os
import re as _re_mod
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

FIRST_LIGHT_VERSION = "0.3.0"

# Valid provenance values — the only three that may appear in evidence.json.
# never_observed      : nothing we ran ever entered this function body.
# observed_in_situ    : the system executed it on its own, under normal operation.
#                       When a driver was also written for this unit but a baseline
#                       now reaches it too, the driver is recorded in the unit's
#                       ``driver_redundant_baseline`` attribute; the provenance stays
#                       observed_in_situ because the *observation* is genuine — only
#                       the driver became redundant.
# observed_under_driver: it only ran because we built something to reach it.
PROVENANCE_NEVER           = "never_observed"
PROVENANCE_IN_SITU         = "observed_in_situ"
PROVENANCE_UNDER_DRIVER    = "observed_under_driver"
# PROVENANCE_SUPERSEDED is kept as a read-time alias for backward-compat migration only.
PROVENANCE_SUPERSEDED      = "superseded"


# ---------------------------------------------------------------------------
# Refusal classes — every distinct way a driver can fail the promotion gate.
# ---------------------------------------------------------------------------

class RefusalClass(str, enum.Enum):
    """Machine-readable reason a driver was refused promotion.

    Each member maps to one distinct branch in cmd_promote_driver.  A unit
    that was attempted but not promoted carries ``refusal_class`` (this enum
    value) and ``refusal_reason`` (the human-readable string) in evidence.json.
    """
    # The driver's filename stem did not match any unit key in evidence.json.
    # This is a configuration error, not a gate failure.
    unit_not_found           = "unit_not_found"

    # The driver process returned a non-zero exit code.
    driver_exited_nonzero    = "driver_exited_nonzero"

    # coverage json export failed after the driver ran.
    coverage_export_failed   = "coverage_export_failed"

    # The driver ran and exited cleanly but no line inside the function body
    # was executed.
    body_never_reached       = "body_never_reached"

    # The driver has no "# call site:" comment — it declared nothing to verify.
    no_call_site             = "no_call_site"

    # An indirect call site comment that does not contain exactly two
    # semicolon-separated segments.
    indirect_wrong_format    = "indirect_wrong_format"

    # The call site text does not parse as "file:N (-- ...)" form.
    call_site_not_file_line  = "call_site_not_file_line"

    # The file named in the call site comment does not exist on disk.
    call_site_file_not_found = "call_site_file_not_found"

    # The file exists but is not inside the package under analysis.
    call_site_outside_package = "call_site_outside_package"

    # The line number named in the call site exceeds the file's length.
    call_site_line_out_of_range = "call_site_line_out_of_range"

    # The function's simple name does not appear on the cited line.
    name_not_on_line         = "name_not_on_line"

    # The cited line exists and contains the name, but is a definition,
    # import, decorator, __all__ declaration, or comment — not a use.
    line_not_a_call_site     = "line_not_a_call_site"

    # The cited line falls inside the target function's own body range.
    call_site_inside_own_body = "call_site_inside_own_body"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class FuncInfo(NamedTuple):
    qualified_name: str   # e.g. "visidata.column.Column.getValue"
    file: str             # absolute path
    def_line: int         # line of the `def` keyword
    body_start: int       # first line of the body (node.body[0].lineno)
    body_end: int         # last line of the body (node.end_lineno)
    is_async: bool


# ---------------------------------------------------------------------------
# Step 1: collect executed lines via coverage.py
# ---------------------------------------------------------------------------

# Wrapper template injected around the user's runner script.
# It patches os._exit → SystemExit so that any target program that calls
# os._exit() (bypassing atexit handlers) cannot prevent coverage.py from
# flushing its data file.  A warning is printed to stderr when triggered,
# because os._exit() in a target is itself a noteworthy finding.
_WRAPPER_TEMPLATE = '''\
import os as _os
import sys as _sys

_real_exit = _os._exit

def _patched_exit(code):
    import sys as _s
    print(
        "[first_light] WARNING: target called os._exit(" + str(code) + "); "
        "intercepted to allow coverage flush",
        file=_s.stderr,
        flush=True,
    )
    raise SystemExit(code)

_os._exit = _patched_exit

try:
    with open({runner_path!r}) as _f:
        _src = _f.read()
    exec(compile(_src, {runner_path!r}, "exec"), {{"__file__": {runner_path!r}, "__name__": "__main__"}})
except SystemExit:
    raise
finally:
    _os._exit = _real_exit
'''


def collect_coverage(
    package_path: str,
    runner_script: str,
    python: str,
    rcfile: str | None = None,
) -> tuple[dict[str, set[int]], int, str]:
    """Run *runner_script* under coverage.py and return ({abs_path: {lines}}, exit_code, stdout).

    A thin wrapper script is written to a temp file before invoking coverage.
    The wrapper patches os._exit → SystemExit so that targets which call
    os._exit() (like visidata's vd_cli) cannot prevent coverage from writing
    its data file.  A warning is emitted to stderr when the patch fires.

    Returns a tuple of (executed_lines_map, runner_exit_code, runner_stdout).
    The exit code is the raw return code of the runner subprocess (after the
    wrapper); it is recorded verbatim in the baseline so the evidence is
    transparent about partial runs (e.g. a test suite where some tests failed
    on this platform).  The stdout is returned so callers can parse runner-
    specific output (e.g. pytest collected/passed/failed counts).
    """
    pkg_abs = str(Path(package_path).resolve())
    runner_abs = str(Path(runner_script).resolve())

    with tempfile.TemporaryDirectory(prefix="first_light_cov_") as tmpdir:
        data_file = os.path.join(tmpdir, ".coverage")

        # Write the wrapper that patches os._exit and then exec's the runner.
        wrapper_path = os.path.join(tmpdir, "_fl_wrapper.py")
        with open(wrapper_path, "w") as wf:
            wf.write(_WRAPPER_TEMPLATE.format(runner_path=runner_abs))

        cmd = [
            python, "-m", "coverage", "run",
            f"--source={pkg_abs}",
            f"--data-file={data_file}",
            # No --branch flag: line coverage only.
        ]
        if rcfile:
            cmd += [f"--rcfile={rcfile}"]

        cmd.append(wrapper_path)

        result = subprocess.run(cmd, capture_output=True, text=True)
        runner_exit_code = result.returncode
        runner_stdout = result.stdout or ""
        if result.returncode not in (0, 1):
            # rc 1 is acceptable (e.g. visidata batch mode exits 1 on warnings;
            # pytest exits 1 when tests fail — that is expected on Windows).
            print(f"[first_light] coverage run exited {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr[:2000], file=sys.stderr)

        # Export to JSON so we don't need to import coverage internals
        json_path = os.path.join(tmpdir, "coverage.json")
        export_cmd = [
            python, "-m", "coverage", "json",
            f"--data-file={data_file}",
            "-o", json_path,
            "--pretty-print",
        ]
        if rcfile:
            export_cmd += [f"--rcfile={rcfile}"]

        export_result = subprocess.run(export_cmd, capture_output=True, text=True)
        if export_result.returncode != 0:
            print("[first_light] coverage json export failed:", file=sys.stderr)
            print(export_result.stderr[:2000], file=sys.stderr)
            return {}, runner_exit_code, runner_stdout

        with open(json_path) as fh:
            data = json.load(fh)

        executed: dict[str, set[int]] = {}
        for file_path, file_data in data.get("files", {}).items():
            abs_path = str(Path(file_path).resolve())
            executed_lines = set(file_data.get("executed_lines", []))
            executed[abs_path] = executed_lines

    return executed, runner_exit_code, runner_stdout


def _parse_pytest_counts(stdout: str) -> tuple[int | None, int | None, int | None]:
    """Parse collected/passed/failed counts from pytest's summary line.

    pytest prints a line like:
        "5 passed, 2 failed in 3.14s"
        "7 passed in 1.23s"
        "3 failed in 0.45s"
        "10 passed, 2 warnings in 5.00s"
    (or a '=' separator line when --no-header is used).

    Returns (collected, passed, failed) as ints, or (None, None, None) if
    the summary line is not found.
    """
    import re as _re
    passed = failed = collected = None
    for line in reversed(stdout.splitlines()):
        m_passed = _re.search(r'(\d+)\s+passed', line)
        m_failed = _re.search(r'(\d+)\s+failed', line)
        m_error  = _re.search(r'(\d+)\s+error', line)
        if m_passed or m_failed or m_error:
            passed  = int(m_passed.group(1)) if m_passed else 0
            failed  = int(m_failed.group(1)) if m_failed else 0
            if m_error:
                failed = (failed or 0) + int(m_error.group(1))
            # "collected N items" line appears earlier
            for prev_line in stdout.splitlines():
                m_coll = _re.search(r'collected\s+(\d+)\s+item', prev_line)
                if m_coll:
                    collected = int(m_coll.group(1))
                    break
            if collected is None:
                collected = (passed or 0) + (failed or 0)
            return collected, passed, failed
    return None, None, None


# ---------------------------------------------------------------------------
# Step 2: enumerate all functions via ast
# ---------------------------------------------------------------------------

def iter_functions(source_path: Path, pkg_root: Path) -> list[FuncInfo]:
    """Parse *source_path* and yield FuncInfo for every function definition."""
    try:
        source = source_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(source_path))
    except SyntaxError:
        return []

    rel = source_path.relative_to(pkg_root.parent)
    # Build a dotted module prefix: visidata/column.py -> visidata.column
    module_parts = list(rel.with_suffix("").parts)
    if module_parts[-1] == "__init__":
        module_parts = module_parts[:-1]
    module_name = ".".join(module_parts)

    funcs: list[FuncInfo] = []

    def _visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Build the qualified name
                qname = f"{scope}.{child.name}" if scope else child.name
                # body_start: first statement of the body
                if child.body:
                    body_start = child.body[0].lineno
                else:
                    body_start = child.lineno + 1  # degenerate: empty body
                body_end = child.end_lineno or body_start

                funcs.append(FuncInfo(
                    qualified_name=qname,
                    file=str(source_path.resolve()),
                    def_line=child.lineno,
                    body_start=body_start,
                    body_end=body_end,
                    is_async=isinstance(child, ast.AsyncFunctionDef),
                ))
                # recurse into nested functions / methods
                _visit(child, qname)
            elif isinstance(child, ast.ClassDef):
                class_scope = f"{scope}.{child.name}" if scope else child.name
                _visit(child, class_scope)
            else:
                _visit(child, scope)

    _visit(tree, module_name)
    return funcs


# ---------------------------------------------------------------------------
# Step 3: decide observed / never-observed
# ---------------------------------------------------------------------------

def classify_functions(
    funcs: list[FuncInfo],
    executed: dict[str, set[int]],
) -> tuple[list[FuncInfo], list[FuncInfo]]:
    """Return (observed, never_observed) lists."""
    observed = []
    never_observed = []

    for fn in funcs:
        file_lines = executed.get(fn.file, set())
        # A function is observed if ANY line in [body_start, body_end] was executed.
        if file_lines.intersection(range(fn.body_start, fn.body_end + 1)):
            observed.append(fn)
        else:
            never_observed.append(fn)

    return observed, never_observed


# ---------------------------------------------------------------------------
# Step 4: collect source files, with exclusion support
# ---------------------------------------------------------------------------

ALWAYS_EXCLUDE = {"__pycache__", ".git", ".tox", ".venv", "venv"}

def collect_python_files(
    pkg_path: Path,
    exclude_dirs: set[str],
) -> list[Path]:
    """Recursively collect .py files under *pkg_path*, skipping *exclude_dirs*."""
    all_dirs = ALWAYS_EXCLUDE | exclude_dirs
    files = []
    for py_file in sorted(pkg_path.rglob("*.py")):
        parts = set(py_file.relative_to(pkg_path).parts)
        if parts & all_dirs:
            continue
        files.append(py_file)
    return files


# ---------------------------------------------------------------------------
# Step 5: build per-directory breakdown
# ---------------------------------------------------------------------------

def directory_label(file: str, pkg_root: Path) -> str:
    """Return a short display label for the directory containing *file*."""
    rel = Path(file).relative_to(pkg_root)
    parts = rel.parts
    if len(parts) == 1:
        return "(root)"
    return parts[1] if len(parts) == 2 else "/".join(parts[1:-1])


def build_breakdown(
    funcs: list[FuncInfo],
    observed_set: set[int],  # ids of observed FuncInfo objects
    pkg_root: Path,
) -> dict[str, dict]:
    """Return per-directory stats dict."""
    by_dir: dict[str, dict] = defaultdict(lambda: {"total": 0, "observed": 0, "never": 0})
    for fn in funcs:
        label = directory_label(fn.file, pkg_root)
        by_dir[label]["total"] += 1
        if id(fn) in observed_set:
            by_dir[label]["observed"] += 1
        else:
            by_dir[label]["never"] += 1
    return dict(sorted(by_dir.items()))


# ---------------------------------------------------------------------------
# evidence.json writer
# ---------------------------------------------------------------------------

def _sha256(path: str) -> str:
    """Return the hex SHA-256 of a file's current contents."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class BaselineInfo:
    """All the metadata and coverage results for one baseline run."""
    def __init__(
        self,
        baseline_id: str,
        runner_script: str,
        cmd: list[str],
        exit_code: int,
        executed: dict[str, set[int]],
        pytest_collected: int | None = None,
        pytest_passed: int | None = None,
        pytest_failed: int | None = None,
        runner_cmd: list[str] | None = None,
    ) -> None:
        self.id = baseline_id
        self.runner_script = runner_script
        self.cmd = cmd          # full first_light.py invocation (kept for back-compat)
        self.runner_cmd = runner_cmd or cmd  # the actual command this specific runner executed
        self.exit_code = exit_code
        self.executed = executed  # {abs_path: {executed_line_numbers}}
        # Optional pytest counts (None when the runner is not pytest)
        self.pytest_collected = pytest_collected
        self.pytest_passed = pytest_passed
        self.pytest_failed = pytest_failed


def write_evidence(
    out_path: str,
    pkg_path: Path,
    exclude_dirs: set[str],
    all_funcs: list[FuncInfo],
    baselines: list[BaselineInfo],
) -> None:
    """Write the evidence.json artifact to *out_path*.

    Schema fields (v0.3.0)
    ----------------------
    generated_at        : ISO-8601 UTC timestamp.
    first_light_version : semver string from FIRST_LIGHT_VERSION.
    baselines           : list of baseline objects, each with:
      id                : short identifier string ("cli", "test_suite", …).
      runner            : absolute path to the runner script.
      package           : absolute path to the analysed package.
      command           : argv list actually executed by THIS baseline's runner
                          (i.e. the coverage-run command for this specific runner).
      exit_code         : raw exit code of the runner subprocess.
      excluded_dirs     : directory names excluded from the product-code figure.
      pytest_collected  : (optional) number of tests collected, when runner is pytest.
      pytest_passed     : (optional) number of tests that passed.
      pytest_failed     : (optional) number of tests that failed.
    integrity           : per-file SHA-256 of every analysed source file.
                          The top-level ``stale`` boolean is computed on read
                          by fl_hook.py — it is NOT stored here (it's a
                          derived property, not an observation).
    units               : dict keyed by ``module::qualname``.
      file              : absolute path to the source file.
      def_line          : line number of the ``def`` keyword.
      body_start        : first line of the body (node.body[0].lineno).
      body_end          : last line of the body (node.end_lineno).
      provenance        : one of PROVENANCE_NEVER | PROVENANCE_IN_SITU |
                          PROVENANCE_UNDER_DRIVER.
                          A unit is observed_in_situ when ANY baseline observed
                          it (unless promoted to observed_under_driver).
                          When a driver was written for a unit that a baseline
                          now also reaches in situ, the driver is recorded in
                          ``driver_redundant_baseline`` rather than changing
                          provenance — the observation is genuine.
      observed_in_baseline : list of baseline ids that observed this unit.
                          An empty list means never_observed.
      driver_redundant_baseline : (optional) baseline id that made a previously
                          attempted driver redundant. Present only on
                          observed_in_situ units for which a driver file exists.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # ── integrity: hash every source file that was analysed ──────────────
    seen_files: set[str] = {fn.file for fn in all_funcs}
    integrity: dict[str, str] = {}
    for fpath in sorted(seen_files):
        try:
            integrity[fpath] = _sha256(fpath)
        except OSError:
            integrity[fpath] = ""

    # ── per-baseline observed-id sets ────────────────────────────────────
    # For each baseline, compute which FuncInfo ids were observed.
    baseline_obs: dict[str, set[int]] = {}
    for bl in baselines:
        obs, _ = classify_functions(all_funcs, bl.executed)
        baseline_obs[bl.id] = {id(fn) for fn in obs}

    # ── units: one entry per function ────────────────────────────────────
    units: dict[str, dict] = {}
    for fn in all_funcs:
        key = f"{fn.file}::{fn.qualified_name}#{fn.def_line}"
        if key in units:
            print(
                f"[first_light] WARNING: unit key collision on {key!r} — "
                f"two functions share the same file, qualified name, and def_line. "
                f"One entry will be overwritten. This is a bug in the analyser.",
                file=sys.stderr,
            )
        fn_id = id(fn)
        observed_in = [bl.id for bl in baselines if fn_id in baseline_obs[bl.id]]
        provenance = PROVENANCE_IN_SITU if observed_in else PROVENANCE_NEVER
        units[key] = {
            "file": fn.file,
            "def_line": fn.def_line,
            "body_start": fn.body_start,
            "body_end": fn.body_end,
            "provenance": provenance,
            "observed_in_baseline": observed_in,
        }

    # ── baselines list ────────────────────────────────────────────────────
    pkg_abs = str(pkg_path.resolve())
    baselines_doc = []
    for bl in baselines:
        entry: dict = {
            "id": bl.id,
            "runner": str(Path(bl.runner_script).resolve()),
            "package": pkg_abs,
            "command": bl.runner_cmd,  # per-runner actual command, not the shared parent argv
            "exit_code": bl.exit_code,
            "excluded_dirs": sorted(exclude_dirs),
        }
        if bl.pytest_collected is not None:
            entry["pytest_collected"] = bl.pytest_collected
        if bl.pytest_passed is not None:
            entry["pytest_passed"] = bl.pytest_passed
        if bl.pytest_failed is not None:
            entry["pytest_failed"] = bl.pytest_failed
        baselines_doc.append(entry)

    doc = {
        "generated_at": now,
        "first_light_version": FIRST_LIGHT_VERSION,
        "baselines": baselines_doc,
        "integrity": integrity,
        "units": units,
    }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)


# ---------------------------------------------------------------------------
# HTML report generator  (reads evidence.json, writes self-contained HTML)
# ---------------------------------------------------------------------------

def _driver_call_site(driver_path: Path) -> tuple[str, bool]:
    """Extract the call site declaration from a driver file.

    Returns (call_site_text, is_indirect) where:
      call_site_text : the text following "call site:" or "call site (indirect):",
                       or '' if no such comment exists.
      is_indirect    : True when the comment begins with "call site (indirect):".

    Indirect call site format (two semicolon-separated parts):
        # call site (indirect): file:N -- dispatch_code ; file:M -- binding_name

    Direct call site format (single part, unchanged):
        # call site: file:N -- funcname(...)
    """
    try:
        text = driver_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            lower = stripped.lower()
            if lower.startswith("call site (indirect):"):
                return stripped[len("call site (indirect):"):].strip(), True
            if lower.startswith("call site:"):
                return stripped[len("call site:"):].strip(), False
        # Fallback: look for 'Real call site' / 'call site' in docstring lines
        for line in text.splitlines():
            lower_line = line.lower()
            if "call site" in lower_line and "--" in line:
                after = line[line.index("--"):].strip(" -")
                if after:
                    return after, False
    except OSError:
        pass
    return "", False


_HTML_JS = """
const R = document.documentElement;
const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
R.classList.add('js-on');

/* The headline resolves from noise into the figure. It is the one piece of
   motion here that carries meaning rather than polish: something that could
   not be read becoming readable is what the project is named after. */
(function resolveHeadline() {
  const el = document.querySelector('.headline__never');
  if (!el || reduce) return;
  const target = el.textContent.trim();
  /* The scramble must never read as a number. Digits would mean the headline
     shows a plausible but wrong figure for up to 600ms, and a screenshot or a
     video frame taken in that window would publish it. Noise that cannot be
     mistaken for data is the only safe alphabet here. */
  const glyphs = '/|_-=+*#~';
  const start = performance.now();
  const DURATION = 600;
  function frame(now) {
    const t = Math.min(1, (now - start) / DURATION);
    let out = '';
    for (let k = 0; k < target.length; k++) {
      const settleAt = (k + 1) / target.length;
      out += t >= settleAt
        ? target[k]
        : glyphs[(Math.random() * glyphs.length) | 0];
    }
    el.textContent = out;
    if (t < 1) requestAnimationFrame(frame);
    else el.textContent = target;
  }
  requestAnimationFrame(frame);
})();

/* Filtering. The map was 250 rows with no way in; this makes the number
   in the headline something you can walk to. */
(function filters() {
  const search = document.getElementById('fl-search');
  const chips  = Array.from(document.querySelectorAll('.chip'));
  const status = document.getElementById('fl-status');
  const rows   = Array.from(document.querySelectorAll('.map-row'));
  if (!rows.length) return;
  const state = { q: '', only: 'all' };

  function apply() {
    let shown = 0, never = 0;
    rows.forEach(row => {
      const name = (row.dataset.name || '').toLowerCase();
      const nNever = parseInt(row.dataset.never || '0', 10);
      const nTotal = parseInt(row.dataset.total || '0', 10);
      const inScope = row.dataset.scope === 'product';
      let ok = !state.q || name.indexOf(state.q) !== -1;
      if (ok && state.only === 'never')   ok = nNever > 0;
      if (ok && state.only === 'clean')   ok = nNever === 0 && nTotal > 0;
      if (ok && state.only === 'product') ok = inScope;
      row.hidden = !ok;
      if (ok) { shown++; never += nNever; }
    });
    if (status) {
      status.textContent = shown + ' of ' + rows.length + ' modules, '
                         + never + ' never observed';
    }
  }

  if (search) {
    search.addEventListener('input', () => {
      state.q = search.value.trim().toLowerCase();
      apply();
    });
  }
  chips.forEach(c => {
    c.addEventListener('click', () => {
      const v = c.dataset.only;
      state.only = (state.only === v) ? 'all' : v;
      chips.forEach(o => o.setAttribute('aria-pressed',
        String(o.dataset.only === state.only)));
      apply();
    });
  });
  apply();
})();
"""


_HTML_CSS = """\
/* ============================================================
   FIRST LIGHT -- report styles
   All CSS lives here. Layout and content are in the <body>.
   ============================================================ */

:root {
  --bg:          #1A1714;
  --surface:     #211E1B;
  --border:      #312C27;
  --text:        #E8E0D5;
  --muted:       #8A7F74;
  --amber:       #C8761A;
  --amber-dim:   #7A4810;
  --never:       #4A423A;
  --never-border:#6B6055;
  --redundant:   #8A6A1E;
  --redundant-br:#C8A44A;
  --cell-size:   10px;
  --font-mono:   "SFMono-Regular", "Consolas", "Liberation Mono", monospace;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html { background: var(--bg); }

body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.6;
  font-variant-numeric: tabular-nums lining-nums slashed-zero;
  max-width: 1060px;
  margin: 0 auto;
  padding: 48px 24px 80px;
}

/* ── Typography ─────────────────────────────────────────── */
.label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}

/* ── Headline block ─────────────────────────────────────── */
.headline {
  margin-bottom: 40px;
}

.headline__never {
  font-size: 96px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -3px;
  color: var(--text);
  display: block;
}

.headline__word {
  font-size: 18px;
  font-weight: 400;
  color: var(--muted);
  display: block;
  margin-top: 4px;
  margin-left: 4px;
}

.headline__context {
  display: flex;
  gap: 40px;
  margin-top: 20px;
  margin-left: 4px;
}

.ctx-item__num {
  font-size: 28px;
  font-weight: 600;
  color: var(--text);
  display: block;
  line-height: 1;
}

.ctx-item__amber .ctx-item__num {
  color: var(--amber);
}

.ctx-item__word {
  font-size: 11px;
  color: var(--muted);
  display: block;
  margin-top: 2px;
}

/* ── Dual-scope panel (product-code vs whole-package) ───── */
/* A flex wrap put three cards on one row and stranded the fourth beside half a
   page of dead space, which split the two baseline cards that exist to be read
   against each other. A grid keeps the scope pair on one line and the baseline
   pair on the next, at every width that fits two. */
.scope-panel {
  display: grid;
  grid-template-columns: repeat(2, minmax(260px, 1fr));
  gap: 24px 32px;
  margin-top: 24px;
  margin-left: 4px;
  align-items: start;
}

@media (max-width: 640px) {
  .scope-panel { grid-template-columns: 1fr; }
}

.scope-block {
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 14px 20px;
  min-width: 180px;
}

.scope-block__label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 8px;
}

.scope-block__row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  font-family: var(--font-mono);
  margin-top: 4px;
}

.scope-block__row--highlight .scope-block__val {
  color: var(--amber);
  font-weight: 600;
}

.scope-block__key {
  color: var(--muted);
}

.scope-block__val {
  color: var(--text);
}

.scope-block--product {
  border-color: var(--amber-dim);
}

.scope-note {
  font-size: 11px;
  color: var(--muted);
  margin-top: 8px;
  margin-left: 4px;
}

/* ── Separator ──────────────────────────────────────────── */
.sep {
  border: none;
  border-top: 1px solid var(--border);
  margin: 36px 0;
}

/* ── Baseline block ─────────────────────────────────────── */
.baseline {
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 20px 24px;
  margin-bottom: 40px;
}

.baseline__grid {
  display: grid;
  grid-template-columns: 100px 1fr;
  row-gap: 8px;
  column-gap: 16px;
  margin-top: 12px;
}

.baseline__key {
  color: var(--muted);
  font-size: 12px;
  font-family: var(--font-mono);
  padding-top: 1px;
}

.baseline__val {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text);
  word-break: break-all;
}

.baseline__val--cmd {
  color: var(--amber);
}

/* ── Map section ────────────────────────────────────────── */
.map-section {
  margin-bottom: 48px;
}

.section-heading {
  font-size: 13px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  margin-bottom: 20px;
}

.legend {
  display: flex;
  gap: 24px;
  margin-bottom: 20px;
  align-items: center;
}

.legend__item {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--muted);
}

/* Legend swatches */
.swatch {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

.swatch--never {
  background: var(--never);
  border: 1px solid var(--never-border);
}

.swatch--insitu {
  background: var(--amber);
}

.swatch--driver {
  background: var(--amber-dim);
  /* hatching pattern using CSS gradient stripes */
  background-image: repeating-linear-gradient(
    45deg,
    transparent,
    transparent 2px,
    rgba(200,118,26,0.45) 2px,
    rgba(200,118,26,0.45) 4px
  );
  background-color: var(--amber-dim);
  border: 1px solid var(--amber);
}

.swatch--redundant {
  background: #2a2a12;
  border: 1px solid #b8a030;
  outline: 2px solid #3a3a20;
  outline-offset: -3px;
}

/* Map rows */
.map-row {
  display: flex;
  align-items: flex-start;
  margin-bottom: 8px;
  gap: 12px;
}

.map-row__label {
  width: 260px;
  flex-shrink: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  padding-top: 1px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.map-row__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

/* The count is the actionable fact in the row. Without it a file that is 75
   unobserved out of 82 looks the same as a four-function stub. */
.map-row__count {
  flex-shrink: 0;
  font-variant-numeric: tabular-nums lining-nums;
  font-size: 10px;
  letter-spacing: 0.02em;
}

.map-row__count--high { color: #C89C6A; }
.map-row__count--none { color: #5A5149; }

/* Rows outside the product scope are in the map but not in the headline
   figure. Saying so is cheaper than letting someone count squares and get a
   different number. */
.map-row--excluded .map-row__name { opacity: 0.55; }
.map-row--excluded /* The map was 250 rows tall, which pushed the driver results, the refusal
   distribution and the footer so far down that nobody reached them. It now
   scrolls inside a bounded box: the whole population is still there, and the
   rest of the report is one screen away instead of nine. */
.map-body {
  /* Fixed pixels rather than vh: a viewport unit resolves to zero in some
     embedded viewers, which would collapse the map to nothing. This is about
     seventy percent of a 1080p screen and behaves identically everywhere. */
  max-height: 720px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 8px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}

.map-body::-webkit-scrollbar { width: 10px; }
.map-body::-webkit-scrollbar-track { background: transparent; }
.map-body::-webkit-scrollbar-thumb {
  background: var(--border);
  border: 3px solid var(--bg);
  border-radius: 6px;
}
.map-body::-webkit-scrollbar-thumb:hover { background: var(--muted); }
.map-body { scrollbar-width: thin; scrollbar-color: var(--border) transparent; }

.map-hint {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.05em;
  color: var(--muted);
  padding: 8px 0 0;
}

.map-row__cells { opacity: 0.5; }

.map-row__scope {
  font-size: 9px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #6B6055;
  flex-shrink: 0;
}

@media (max-width: 720px) {
  .map-row { flex-direction: column; gap: 4px; }
  .map-row__label { width: auto; }
}

.map-row__cells {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  flex: 1;
}

.cell {
  width: var(--cell-size);
  height: var(--cell-size);
  flex-shrink: 0;
  position: relative;
  transition: transform 120ms cubic-bezier(0.23, 1, 0.32, 1);
}

/* A cell is a function. Hovering one should feel like pointing at it, and the
   tooltip that was already there becomes discoverable instead of accidental. */
.cell:hover {
  transform: scale(1.6);
  z-index: 3;
  box-shadow: 0 0 0 1px var(--text);
}

/* Hovering a row dims the rest, so 250 rows of texture become one row you are
   actually reading. */
.map-body:hover .map-row { opacity: 0.45; }
.map-body:hover .map-row:hover { opacity: 1; }
.map-row { transition: opacity 120ms linear; }

.map-row:hover .map-row__name { color: var(--text); }
.map-row:hover .map-row__count--high { color: var(--amber); }

.cell--never {
  background: var(--never);
  border: 1px solid var(--never-border);
}

.cell--insitu {
  background: var(--amber);
  border: 1px solid var(--amber);
}

.cell--driver {
  background-color: var(--amber-dim);
  background-image: repeating-linear-gradient(
    45deg,
    transparent,
    transparent 2px,
    rgba(200,118,26,0.55) 2px,
    rgba(200,118,26,0.55) 4px
  );
  border: 1px solid var(--amber);
}

.cell--redundant {
  background: var(--redundant);
  border: 1px solid var(--redundant-br);
}

.swatch--redundant {
  background: var(--redundant);
  border: 1px solid var(--redundant-br);
}

/* ── Drivers section ────────────────────────────────────── */
.drivers-section {
  margin-bottom: 48px;
}

.driver-card {
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 16px 20px;
  margin-bottom: 10px;
}

.driver-card__name {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 600;
  color: var(--amber);
  margin-bottom: 4px;
}

.driver-card__call {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  word-break: break-all;
}

.driver-card__lines {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  margin-top: 4px;
}

/* ── Footer ─────────────────────────────────────────────── */
.footer {
  margin-top: 64px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
  text-align: center;
  font-size: 11px;
  color: var(--muted);
}

/* ── Entrance choreography ──────────────────────────────────
   Only transform and opacity, so nothing here can cause layout work.
   Every duration is under 300ms. The whole thing is off under
   prefers-reduced-motion, and the page renders complete without it: the
   animation is an enhancement, never a gate on the content. */
/* The hidden state lives under .js-on and nowhere else. A bare .rise that set
   opacity to 0 would make the whole page invisible whenever the script did not
   run, which is the opposite of an enhancement. */
.js-on .rise {
  opacity: 0;
  transform: translateY(6px);
  animation: rise 240ms cubic-bezier(0.23, 1, 0.32, 1) forwards;
  animation-delay: calc(var(--i, 0) * 60ms);
}

@keyframes rise {
  to { opacity: 1; transform: none; }
}

/* Map rows draw in as they enter the viewport, capped so a long file does
   not turn into a slow crawl. */
/* ── Sticky toolbar ─────────────────────────────────────── */
.toolbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  padding: 12px 0;
  margin-bottom: 20px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
}

.toolbar__search {
  flex: 1 1 200px;
  min-width: 160px;
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 7px 10px;
}

.toolbar__search::placeholder { color: var(--muted); }
.toolbar__search:focus-visible {
  outline: 2px solid var(--amber);
  outline-offset: 1px;
}

.chip {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--muted);
  background: transparent;
  border: 1px solid var(--border);
  padding: 6px 10px;
  cursor: pointer;
}

.chip:hover { color: var(--text); border-color: var(--muted); }
.chip:focus-visible { outline: 2px solid var(--amber); outline-offset: 1px; }
.chip[aria-pressed="true"] {
  color: var(--bg);
  background: var(--amber);
  border-color: var(--amber);
}

.chip__count {
  font-variant-numeric: tabular-nums lining-nums;
  opacity: 0.75;
  margin-left: 6px;
}

.toolbar__status {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  font-variant-numeric: tabular-nums lining-nums;
  margin-left: auto;
}

.map-row[hidden] { display: none; }

/* ── Section navigation ─────────────────────────────────── */
.topnav {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  padding: 10px 0 22px;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.topnav a { color: var(--muted); text-decoration: none; }
.topnav a:hover { color: var(--amber); }
.topnav a:focus-visible { outline: 2px solid var(--amber); outline-offset: 2px; }

/* Visible to a screen reader, not on screen. The search input needs a real
   label; a placeholder is not one. */
.sr-only {
  position: absolute;
  width: 1px; height: 1px;
  padding: 0; margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}

.headline__never { will-change: contents; }

/* The context figures were inert. A pointer on one should make it the one you
   are reading, which is most of what "dynamic" means on a page of numbers. */
.ctx-item {
  transition: transform 120ms cubic-bezier(0.23, 1, 0.32, 1);
  cursor: default;
}
.ctx-item:hover { transform: translateY(-2px); }
.ctx-item:hover .ctx-item__num { color: var(--amber); }
.ctx-item__num { transition: color 120ms linear; }

.scope-block { transition: border-color 140ms linear; }
.scope-block:hover { border-color: var(--muted); }

.driver-card { transition: border-color 140ms linear, transform 120ms cubic-bezier(0.23,1,0.32,1); }
.driver-card:hover { border-color: var(--muted); transform: translateY(-1px); }

@media (prefers-reduced-motion: reduce) {
  .cell, .cell:hover, .ctx-item, .ctx-item:hover,
  .driver-card, .driver-card:hover, .map-row {
    transition: none !important;
    transform: none !important;
  }
}

@media (prefers-reduced-motion: reduce) {
  .rise, .js-on .rise {
    opacity: 1 !important;
    transform: none !important;
    animation: none !important;
  }
}
"""


def _strip_def_line_suffix(qname: str) -> str:
    """Strip the '#<lineno>' suffix appended to unit keys before showing to humans."""
    if "#" in qname:
        return qname.rsplit("#", 1)[0]
    return qname


def _driver_units_from_evidence(
    units: dict,
    drivers_dir: Path | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (confirmed, redundant, attempted_names).

    confirmed      : list of dicts with keys name, call_site, coverage_confirmed_lines
                     for every unit whose provenance is observed_under_driver.
    redundant      : list of dicts for every observed_in_situ unit that has a
                     ``driver_redundant_baseline`` attribute — the driver existed
                     but a baseline now also reaches the unit in situ.
                     Also accepts legacy provenance==superseded entries from
                     evidence.json files written by older versions.
    attempted_names: list of qualified names whose driver file exists on disk but
                     whose unit is NOT observed_under_driver or redundant in evidence
                     — i.e. drivers that were attempted but not confirmed.
    """
    confirmed: list[dict] = []
    redundant: list[dict] = []
    confirmed_or_redundant_qnames: set[str] = set()

    for key, u in units.items():
        prov = u.get("provenance")
        qname_part = key.split("::", 1)[1] if "::" in key else key
        qname_part = _strip_def_line_suffix(qname_part)

        if prov == PROVENANCE_UNDER_DRIVER:
            confirmed.append({
                "name": qname_part,
                "call_site": u.get("call_site", ""),
                "coverage_confirmed_lines": u.get("coverage_confirmed_lines", []),
            })
            confirmed_or_redundant_qnames.add(qname_part)
        elif prov == PROVENANCE_IN_SITU and u.get("driver_redundant_baseline"):
            # New schema: driver became redundant; provenance stays observed_in_situ.
            redundant.append({
                "name": qname_part,
                "redundant_baseline": u.get("driver_redundant_baseline", ""),
                "call_site": u.get("call_site", ""),
                "coverage_confirmed_lines": u.get("coverage_confirmed_lines", []),
            })
            confirmed_or_redundant_qnames.add(qname_part)
        elif prov == PROVENANCE_SUPERSEDED:
            # Legacy schema (v0.2 evidence.json): accept superseded as redundant.
            redundant.append({
                "name": qname_part,
                "redundant_baseline": u.get("superseded_by", ""),
                "call_site": u.get("call_site", ""),
                "coverage_confirmed_lines": u.get("coverage_confirmed_lines", []),
            })
            confirmed_or_redundant_qnames.add(qname_part)

    # Attempted: driver file exists but unit is not observed_under_driver or redundant.
    attempted_names: list[dict] = []
    if drivers_dir is not None and drivers_dir.is_dir():
        evidence_qnames: dict[str, str] = {}  # qname_suffix -> provenance
        for key, u in units.items():
            if "::" not in key:
                continue
            qname_part_ev = key.split("::", 1)[1]
            qname_part_ev = _strip_def_line_suffix(qname_part_ev)
            evidence_qnames[qname_part_ev] = u.get("provenance", PROVENANCE_NEVER)

        # A driver is "attempted" when its file exists but its unit has no confirmed
        # or redundant entry (not in confirmed_or_redundant_qnames).
        # Index the units by their qualified name so an attempted driver can be
        # reported with what is actually known about it.  "Attempted" is not one
        # outcome: a driver whose lines coverage confirmed and which was then
        # refused for an unverifiable call site is a different fact from one that
        # never reached the function at all, and collapsing them would hide the
        # more interesting of the two.
        units_by_qname: dict[str, dict] = {}
        for key, u in units.items():
            if "::" not in key:
                continue
            units_by_qname[_strip_def_line_suffix(key.split("::", 1)[1])] = u

        for driver_file in sorted(drivers_dir.glob("*.py")):
            stem = driver_file.stem
            if stem not in confirmed_or_redundant_qnames:
                u = units_by_qname.get(stem, {})
                attempted_names.append({
                    "name": stem,
                    # A refused driver may still have reached the function: the
                    # reach and the refusal are recorded separately.
                    "coverage_confirmed_lines": (u.get("driver_reached_lines")
                                                 or u.get("coverage_confirmed_lines")
                                                 or []),
                    "call_site": (u.get("call_site")
                                  or u.get("driver_declared_call_site") or ""),
                })

    # Sort lists for stable output
    confirmed.sort(key=lambda d: d["name"])
    redundant.sort(key=lambda d: d["name"])
    return confirmed, redundant, attempted_names


def write_html_report(evidence_path: str, out_path: str) -> None:
    """Read *evidence_path* (evidence.json) and write a self-contained HTML report to *out_path*."""
    import html as _html

    with open(evidence_path, encoding="utf-8") as fh:
        ev = json.load(fh)

    # Support both old single-baseline schema (v0.1) and new baselines list (v0.2+).
    raw_baselines = ev.get("baselines") or []
    if not raw_baselines and ev.get("baseline"):
        # Legacy v0.1 schema: wrap in a list so downstream code is uniform.
        old = ev["baseline"]
        raw_baselines = [{
            "id": "cli",
            "runner": old.get("runner", ""),
            "package": old.get("package", ""),
            "command": old.get("command", []),
            "exit_code": 0,
            "excluded_dirs": old.get("excluded_dirs", []),
        }]
    units = ev.get("units", {})
    generated_at = ev.get("generated_at", "")
    version = ev.get("first_light_version", "")

    # Use the first baseline's package/excluded_dirs as the canonical reference
    # (all baselines share the same package and excluded list).
    first_bl = raw_baselines[0] if raw_baselines else {}
    excluded_dirs: list[str] = first_bl.get("excluded_dirs", [])

    # ── Compute summary numbers ──────────────────────────────────────────────
    # Whole-package (all units, including tests/apps/experimental/vendor)
    total = len(units)
    never_count = sum(1 for u in units.values() if u["provenance"] == PROVENANCE_NEVER)
    observed_count = total - never_count
    insitu_count = sum(
        1 for u in units.values()
        if u["provenance"] == PROVENANCE_IN_SITU and not u.get("driver_redundant_baseline")
    )
    driver_count = sum(1 for u in units.values() if u["provenance"] == PROVENANCE_UNDER_DRIVER)
    # "redundant" = observed_in_situ units whose driver became redundant, OR legacy superseded
    redundant_count = sum(
        1 for u in units.values()
        if (u["provenance"] == PROVENANCE_IN_SITU and u.get("driver_redundant_baseline"))
        or u["provenance"] == PROVENANCE_SUPERSEDED
    )

    # Product-code only (exclude the same dirs the baseline recorded)
    if excluded_dirs:
        def _is_product(u: dict) -> bool:
            parts = Path(u["file"]).parts
            return not any(part in excluded_dirs for part in parts)
        prod_units = {k: u for k, u in units.items() if _is_product(u)}
    else:
        prod_units = units
    prod_total = len(prod_units)
    prod_never = sum(1 for u in prod_units.values() if u["provenance"] == PROVENANCE_NEVER)
    prod_observed = prod_total - prod_never
    prod_insitu_count = sum(
        1 for u in prod_units.values()
        if u["provenance"] == PROVENANCE_IN_SITU and not u.get("driver_redundant_baseline")
    )
    prod_driver_count = sum(1 for u in prod_units.values() if u["provenance"] == PROVENANCE_UNDER_DRIVER)
    prod_redundant_count = sum(
        1 for u in prod_units.values()
        if (u["provenance"] == PROVENANCE_IN_SITU and u.get("driver_redundant_baseline"))
        or u["provenance"] == PROVENANCE_SUPERSEDED
    )

    # ── Per-baseline observation counts ─────────────────────────────────────
    # For each baseline id, count how many product-code units it observed.
    # Also compute the "additive" count: how many units the Nth baseline observed
    # that none of the preceding baselines observed (cumulative increments).
    bl_ids = [bl["id"] for bl in raw_baselines]

    # product-scope per-baseline counts
    prod_bl_counts: dict[str, int] = {}
    for bl in raw_baselines:
        bl_id = bl["id"]
        prod_bl_counts[bl_id] = sum(
            1 for u in prod_units.values()
            if bl_id in u.get("observed_in_baseline", [])
        )

    # additive contribution: units first seen by each baseline
    # (i.e. observed by this baseline but by NO earlier baseline)
    prod_bl_additive: dict[str, int] = {}
    for bl in raw_baselines:
        bl_id = bl["id"]
        only_here = 0
        for u in prod_units.values():
            obs = u.get("observed_in_baseline", [])
            if obs == [bl_id]:
                only_here += 1
        prod_bl_additive[bl_id] = only_here

    # ── Build per-module map data ────────────────────────────────────────────
    # Group functions by their source file (relative path stripped to module name)
    pkg_path_str = first_bl.get("package", "")
    pkg_root = Path(pkg_path_str) if pkg_path_str else None

    module_map: dict[str, list[dict]] = {}
    for key, u in units.items():
        fpath = Path(u["file"])
        if pkg_root and pkg_root.exists():
            try:
                rel = fpath.relative_to(pkg_root.parent)
                mod_label = str(rel)
            except ValueError:
                mod_label = fpath.name
        else:
            mod_label = fpath.name

        if mod_label not in module_map:
            module_map[mod_label] = []
        raw_qname = key.split("::")[-1] if "::" in key else key
        module_map[mod_label].append({
            "qname": _strip_def_line_suffix(raw_qname),
            "provenance": u["provenance"],
            "driver_redundant_baseline": u.get("driver_redundant_baseline", ""),
            "in_scope": key in prod_units,
        })

    # Sort modules: observed-first, then alphabetically
    def _module_sort_key(item):
        # Lead with the files that carry the headline. Sorting by observed count
        # put the most-covered modules at the top, so a report whose headline is
        # about unobserved code opened on a wall of observed cells and argued
        # against itself for the first screen. Rank by how much of each module
        # has never been observed, and put excluded modules last: they are shown
        # for completeness but they are not part of the figure above.
        label, funcs = item
        never = sum(1 for f in funcs if f["provenance"] == PROVENANCE_NEVER)
        in_scope = bool(funcs) and funcs[0].get("in_scope", True)
        return (0 if in_scope else 1, -never, label)

    sorted_modules = sorted(module_map.items(), key=_module_sort_key)

    # ── Driver info ───────────────────────────────────────────────────────────
    # confirmed : units with provenance=observed_under_driver (coverage verified).
    # redundant : driver existed but a baseline now also covers the unit in situ.
    # attempted : driver files that exist on disk but are not yet confirmed.
    _drivers_dir = Path(__file__).parent / "drivers"
    confirmed_drivers, redundant_drivers, attempted_drivers = _driver_units_from_evidence(units, _drivers_dir)

    # ── Refusal distribution — read refusal_class from evidence units ─────────
    # Collect every unit that carries a refusal_class (refused drivers).
    # A refusal is a finding: it is recorded on the unit in evidence.json by
    # cmd_promote_driver so any reader can query it without re-running drivers.
    refusal_rows: list[tuple[str, str, str]] = []  # (qname, refusal_class, refusal_reason)
    for key, u in units.items():
        rc = u.get("refusal_class")
        if not rc:
            continue
        qname_part = key.split("::", 1)[1] if "::" in key else key
        qname_part = _strip_def_line_suffix(qname_part)
        refusal_rows.append((qname_part, rc, u.get("refusal_reason", "")))
    # Count by class (for the summary table).
    refusal_class_counts: dict[str, int] = {}
    for _, rc, _ in refusal_rows:
        refusal_class_counts[rc] = refusal_class_counts.get(rc, 0) + 1
    total_drivers_attempted = len(confirmed_drivers) + len(redundant_drivers) + len(attempted_drivers)
    # attempted_drivers (as returned above) only covers not-confirmed; for the
    # refusal report we also want the total driver count across all outcomes.
    # We count it from evidence units that have driver_attempted or provenance
    # markers, but the simplest correct count is the sum of all three buckets.

    # ── Relative-path helper ──────────────────────────────────────────────────
    # evidence.json stores absolute paths (needed for hash resolution).  The
    # report displays paths relative to the repository root so it is shareable
    # without leaking local directory structure.
    repo_root = Path(__file__).parent

    def _rel(abs_path: str) -> str:
        """Return *abs_path* relative to repo_root, or basename on failure."""
        try:
            return str(Path(abs_path).relative_to(repo_root))
        except ValueError:
            return Path(abs_path).name

    # ── Build HTML ───────────────────────────────────────────────────────────
    def e(s: str) -> str:
        return _html.escape(str(s))

    excluded = excluded_dirs
    excl_str = ", ".join(excluded) if excluded else "none"
    prod_scope_label = ("Product code (excl. " + excl_str + ")") if excl_str and excl_str != "none" else "Product code"

    # ── Per-baseline cards HTML ───────────────────────────────────────────────
    import re as _re_html
    baseline_cards_html = []
    for bl in raw_baselines:
        bl_id = bl["id"]
        runner_rel = _rel(bl.get("runner", ""))
        package_rel = _rel(bl.get("package", ""))
        command_raw = bl.get("command", [])
        # Shorten paths for display without changing what the command says.
        # _rel() falls back to the bare filename when a path is not under the
        # repo, which turned "--source=<abspath>" into the word "visidata" and
        # printed a command that never ran, under a heading promising the
        # opposite. Only rewrite a token when it actually resolves inside the
        # repo, and rewrite the value of "--flag=path" rather than the whole
        # token.
        def _shorten(tok: str) -> str:
            if "=" in tok and not tok.startswith("="):
                flag, _, val = tok.partition("=")
                if os.sep in val or "/" in val:
                    return f"{flag}={_rel(val)}"
                return tok
            if os.sep in tok or "/" in tok:
                return _rel(tok)
            return tok

        cmd_tokens = [_shorten(str(c)) for c in command_raw]
        cmd_str = " ".join(cmd_tokens) if cmd_tokens else ""
        exit_code = bl.get("exit_code", 0)
        exit_color = "var(--amber)" if exit_code != 0 else "var(--text)"
        partial_note = (
            f' <span style="color:{exit_color};font-weight:600;">[exit {exit_code}, partial run]</span>'
            if exit_code != 0 else ""
        )
        bl_obs = prod_bl_counts.get(bl_id, 0)
        bl_add = prod_bl_additive.get(bl_id, 0)
        add_note = f" ({bl_add} reached by this baseline alone)"
        # pytest counts row (only when the baseline recorded them)
        py_collected = bl.get("pytest_collected")
        py_passed = bl.get("pytest_passed")
        py_failed = bl.get("pytest_failed")
        pytest_row = ""
        if py_collected is not None:
            fail_color = "var(--amber)" if (py_failed or 0) > 0 else "var(--text)"
            pytest_row = (
                f'\n    <span class="baseline__key">tests</span>'
                f'\n    <span class="baseline__val">'
                f'collected {e(str(py_collected))}, '
                f'passed {e(str(py_passed or 0))}, '
                f'<span style="color:{fail_color};">failed {e(str(py_failed or 0))}</span>'
                f'</span>'
            )
        baseline_cards_html.append(f"""
<div class="baseline" style="margin-bottom:16px;">
  <div class="label">Baseline: {e(bl_id)}{partial_note}</div>
  <div class="baseline__grid">
    <span class="baseline__key">runner</span>
    <span class="baseline__val">{e(runner_rel)}</span>

    <span class="baseline__key">package</span>
    <span class="baseline__val">{e(package_rel)}</span>

    <span class="baseline__key">command</span>
    <span class="baseline__val baseline__val--cmd">{e(cmd_str)}</span>

    <span class="baseline__key">exit code</span>
    <span class="baseline__val" style="color:{exit_color};">{e(str(exit_code))}</span>
{pytest_row}
    <span class="baseline__key">observed</span>
    <span class="baseline__val">{e(str(bl_obs))} product-code units{e(add_note)}</span>

    <span class="baseline__key">excluded</span>
    <span class="baseline__val">{e(excl_str)}</span>
  </div>
</div>""")
    all_baselines_html = "\n".join(baseline_cards_html)

    # Map rows HTML
    map_rows_html = []
    for mod_label, funcs in sorted_modules:
        cells = []
        for fn in funcs:
            prov = fn["provenance"]
            is_redundant = prov == PROVENANCE_IN_SITU and fn.get("driver_redundant_baseline")
            if is_redundant:
                cls = "cell cell--redundant"
            elif prov == PROVENANCE_IN_SITU or prov == PROVENANCE_SUPERSEDED:
                cls = "cell cell--insitu"
            elif prov == PROVENANCE_UNDER_DRIVER:
                cls = "cell cell--driver"
            else:
                cls = "cell cell--never"
            title = e(fn["qname"])
            cells.append(f'<span class="{cls}" title="{title}"></span>')
        cells_html = "\n".join(cells)
        label_html = e(mod_label)
        # Count what the row is about, and say whether it counts toward the
        # headline. A row of squares alone is texture. A row that carries its
        # own tally is something a reader can act on, and marking the rows
        # outside the product figure stops anyone counting squares and
        # arriving at a different total than the one printed above.
        n_total   = len(funcs)
        n_never   = sum(1 for f in funcs if f["provenance"] == PROVENANCE_NEVER)
        in_scope  = bool(funcs) and funcs[0].get("in_scope", True)
        count_cls = "map-row__count--none" if n_never == 0 else "map-row__count--high"
        scope_tag = "" if in_scope else '<span class="map-row__scope">excluded</span>'
        row_cls   = "map-row" if in_scope else "map-row map-row--excluded"
        map_rows_html.append(
            f'<div class="{row_cls}" data-name="{label_html}" '
            f'data-never="{n_never}" data-total="{n_total}" '
            f'data-scope="{"product" if in_scope else "excluded"}">'
            f'<span class="map-row__label" title="{label_html}">'
            f'<span class="map-row__name">{label_html}</span>'
            f'{scope_tag}'
            f'<span class="map-row__count {count_cls}" '
            f'title="{n_never} of {n_total} never observed">{n_never}/{n_total}</span>'
            f'</span>'
            f'<div class="map-row__cells">{cells_html}</div>'
            f'</div>'
        )
    map_html = "\n".join(map_rows_html)

    # Driver cards HTML — built from evidence, not from disk file count
    driver_cards_html = []
    for d in confirmed_drivers:
        name_html = e(d["name"])
        confirmed_lines = d["coverage_confirmed_lines"]
        lines_str = (
            ", ".join(str(ln) for ln in confirmed_lines[:6])
            + ("…" if len(confirmed_lines) > 6 else "")
        ) if confirmed_lines else ""
        call_raw = d["call_site"]
        # Relativise absolute paths inside the call site string for display.
        # Only match genuine absolute paths: Windows (C:\... or C:/...) or
        # Unix (/absolute/path).  Relative paths are already short and correct.
        if call_raw:
            import re as _re2
            call_display = _re2.sub(
                r'([A-Za-z]:[\\/][^\s]+|(?<!\w)/[^\s]+)',
                lambda m: _rel(m.group(1)),
                call_raw,
            )
        else:
            call_display = ""
        call_html = e(call_display) if call_display else "<em>call site not recorded</em>"
        lines_html = (
            f'<div class="driver-card__lines">confirmed lines: {e(lines_str)}</div>'
            if lines_str else ""
        )
        driver_cards_html.append(
            f'<div class="driver-card">'
            f'<div class="driver-card__name">{name_html}</div>'
            f'<div class="driver-card__call">{call_html}</div>'
            f'{lines_html}'
            f'</div>'
        )
    drivers_html = "\n".join(driver_cards_html)
    if not drivers_html:
        drivers_html = '<p style="color:var(--muted);font-size:13px;">No units confirmed under driver.</p>'

    # Redundant driver cards (driver existed but baseline now also covers the unit in situ)
    redundant_cards_html = []
    for d in redundant_drivers:
        red_by = d.get("redundant_baseline", "")
        call_raw = d.get("call_site", "")
        if call_raw:
            call_display = _re_html.sub(
                r'([A-Za-z]:[\\/][^\s]+|(?<!\w)/[^\s]+)',
                lambda m: _rel(m.group(1)),
                call_raw,
            )
        else:
            call_display = ""
        call_html = e(call_display) if call_display else "<em>call site not recorded</em>"
        redundant_cards_html.append(
            f'<div class="driver-card" style="border-color:#3a3a20;opacity:0.85;">'
            f'<div class="driver-card__name" style="color:#b8a030;">{e(d["name"])}</div>'
            f'<div class="driver-card__call">{call_html}</div>'
            f'<div class="driver-card__lines" style="margin-top:6px;">'
            f'driver made redundant by baseline: <strong style="color:var(--text);">{e(red_by)}</strong>. '
            f'The unit is observed in situ; the driver is no longer the only evidence'
            f'</div>'
            f'</div>'
        )
    redundant_html = "\n".join(redundant_cards_html)

    # Attempted-but-not-confirmed driver cards
    attempted_cards_html = []
    for d in attempted_drivers:
        _name    = d["name"] if isinstance(d, dict) else str(d)
        _lines   = d.get("coverage_confirmed_lines", []) if isinstance(d, dict) else []
        _cs      = d.get("call_site", "") if isinstance(d, dict) else ""
        if _lines:
            _n = len(_lines)
            _why = ("no call site was declared, so there was no claim to check"
                    if not _cs else
                    "the declared call site could not be confirmed in the source")
            _reason = (
                f"driver ran and coverage confirmed {_n} line"
                f"{'' if _n == 1 else 's'} of the function body; "
                f"promotion refused because {_why}"
            )
        else:
            _reason = "driver ran but no line of the function body executed"
        attempted_cards_html.append(
            f'<div class="driver-card" style="border-color:#5a2020;">'
            f'<div class="driver-card__name" style="color:#cc4444;">{e(_name)}</div>'
            f'<div class="driver-card__call" style="color:var(--muted);">{e(_reason)}</div>'
            f'</div>'
        )
    attempted_html = "\n".join(attempted_cards_html)

    # Legend: show driver swatch when driver evidence exists, redundant swatch when redundant drivers exist
    driver_legend_item = ""
    if driver_count > 0 or redundant_count > 0:
        driver_legend_item = (
            '<div class="legend__item">'
            '<span class="swatch swatch--driver"></span>'
            'observed under driver'
            '</div>'
        )
    redundant_legend_item = ""
    if redundant_count > 0:
        redundant_legend_item = (
            '<div class="legend__item">'
            '<span class="swatch swatch--redundant"></span>'
            'driver made redundant by baseline'
            '</div>'
        )

    # ── Refusal distribution section HTML ────────────────────────────────────
    if refusal_rows:
        total_drivers_count = len(confirmed_drivers) + len(redundant_drivers) + len(attempted_drivers)
        _ref_rows_html = []
        for qname, rc, reason in sorted(refusal_rows, key=lambda t: t[0]):
            _ref_rows_html.append(
                f'<tr>'
                f'<td style="font-family:var(--font-mono);font-size:11px;color:var(--text);padding:6px 8px;border-bottom:1px solid var(--border);">{e(qname)}</td>'
                f'<td style="font-family:var(--font-mono);font-size:11px;color:var(--amber);padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap;">{e(rc)}</td>'
                f'<td style="font-size:11px;color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--border);">{e(reason)}</td>'
                f'</tr>'
            )
        _ref_summary_rows_html = []
        for rc_val, count in sorted(refusal_class_counts.items(), key=lambda kv: -kv[1]):
            _ref_summary_rows_html.append(
                f'<tr>'
                f'<td style="font-family:var(--font-mono);font-size:11px;color:var(--amber);padding:4px 8px;">{e(rc_val)}</td>'
                f'<td style="font-family:var(--font-mono);font-size:13px;font-weight:600;color:var(--text);padding:4px 8px;text-align:right;">{count}</td>'
                f'</tr>'
            )
        _refusal_section_html = f"""<section style="margin-bottom:48px;">
  <div class="section-heading">Refusal distribution: {e(str(total_drivers_count))} drivers measured</div>
  <p style="font-size:12px;color:var(--muted);margin-bottom:16px;">
    {e(str(total_drivers_count))} drivers were measured against this evidence file.
    {e(str(len(confirmed_drivers)))} promoted &nbsp;&middot;&nbsp;
    {e(str(len(redundant_drivers)))} made redundant by a baseline &nbsp;&middot;&nbsp;
    {e(str(len(refusal_rows)))} refused.
    The table shows only classes that occurred.
  </p>
  <table style="border-collapse:collapse;margin-bottom:24px;min-width:320px;">
    <thead>
      <tr>
        <th style="font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);padding:4px 8px;text-align:left;border-bottom:1px solid var(--border);">Refusal class</th>
        <th style="font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);padding:4px 8px;text-align:right;border-bottom:1px solid var(--border);">Count</th>
      </tr>
    </thead>
    <tbody>
      {"".join(_ref_summary_rows_html)}
    </tbody>
  </table>
  <table style="border-collapse:collapse;width:100%;">
    <thead>
      <tr>
        <th style="font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);padding:6px 8px;text-align:left;border-bottom:1px solid var(--border);">Driver</th>
        <th style="font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);padding:6px 8px;text-align:left;border-bottom:1px solid var(--border);">Refusal class</th>
        <th style="font-size:10px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);padding:6px 8px;text-align:left;border-bottom:1px solid var(--border);">Reason</th>
      </tr>
    </thead>
    <tbody>
      {"".join(_ref_rows_html)}
    </tbody>
  </table>
</section>"""
    else:
        _refusal_section_html = ""

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>First Light : Evidence Report</title>
<style>
{_HTML_CSS}
</style>
</head>
<body>

<!-- ═══════════════════════════════════════════════════════
     SECTION 1 — HEADLINE NUMBER
     ═══════════════════════════════════════════════════════ -->
<header class="headline rise" style="--i:0">
  <span class="label">First Light : Function Observation Report</span>
  <nav class="topnav" aria-label="Report sections">
    <a href="#baselines">Baselines</a>
    <a href="#drivers">Driver results</a>
    <a href="#map">Evidence map</a>
  </nav>
  <span class="headline__never">{e(str(prod_never))}</span>
  <span class="headline__word">product-code functions never observed</span>

  <div class="headline__context">
    <div class="ctx-item ctx-item__amber">
      <span class="ctx-item__num">{e(str(prod_observed))}</span>
      <span class="ctx-item__word">observed (product)</span>
    </div>
    <div class="ctx-item">
      <span class="ctx-item__num">{e(str(prod_total))}</span>
      <span class="ctx-item__word">total (product)</span>
    </div>
    <div class="ctx-item">
      <span class="ctx-item__num">{e(str(prod_insitu_count))}</span>
      <span class="ctx-item__word">in-situ</span>
    </div>
    <div class="ctx-item">
      <span class="ctx-item__num">{e(str(prod_driver_count))}</span>
      <span class="ctx-item__word">under driver</span>
    </div>
    <div class="ctx-item">
      <span class="ctx-item__num">{e(str(prod_redundant_count))}</span>
      <span class="ctx-item__word">driver redundant</span>
    </div>
  </div>

  <div class="scope-panel">
    <div class="scope-block scope-block--product">
      <div class="scope-block__label">{e(prod_scope_label)}</div>
      <div class="scope-block__row scope-block__row--highlight">
        <span class="scope-block__key">never observed</span>
        <span class="scope-block__val">{e(str(prod_never))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">in-situ</span>
        <span class="scope-block__val">{e(str(prod_insitu_count))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">under driver</span>
        <span class="scope-block__val">{e(str(prod_driver_count))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">driver redundant</span>
        <span class="scope-block__val">{e(str(prod_redundant_count))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">total</span>
        <span class="scope-block__val">{e(str(prod_total))}</span>
      </div>
    </div>
    <div class="scope-block">
      <div class="scope-block__label">Whole package (incl. tests &amp; vendor)</div>
      <div class="scope-block__row">
        <span class="scope-block__key">never observed</span>
        <span class="scope-block__val">{e(str(never_count))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">in-situ</span>
        <span class="scope-block__val">{e(str(insitu_count))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">under driver</span>
        <span class="scope-block__val">{e(str(driver_count))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">driver redundant</span>
        <span class="scope-block__val">{e(str(redundant_count))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">total</span>
        <span class="scope-block__val">{e(str(total))}</span>
      </div>
    </div>
    {''.join(f"""
    <div class="scope-block">
      <div class="scope-block__label">Baseline: {e(bl["id"])}</div>
      <div class="scope-block__row">
        <span class="scope-block__key">observed</span>
        <span class="scope-block__val">{e(str(prod_bl_counts.get(bl["id"],0)))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">only this one</span>
        <span class="scope-block__val">{e(str(prod_bl_additive.get(bl["id"],0)))}</span>
      </div>
      <div class="scope-block__row">
        <span class="scope-block__key">exit code</span>
        <span class="scope-block__val">{e(str(bl.get("exit_code",0)))}</span>
      </div>
    </div>""" for bl in raw_baselines)}
  </div>
</header>

<hr class="sep">

<!-- ═══════════════════════════════════════════════════════
     SECTION 2 — BASELINES
     ═══════════════════════════════════════════════════════ -->
<section class="rise" style="--i:1" id="baselines">
  <div class="label" style="margin-bottom:12px;">Baselines: exactly what was executed</div>
  <div style="font-size:11px;color:var(--muted);margin-bottom:16px;">
    generated {e(generated_at)} &nbsp;&middot;&nbsp; First Light v{e(version)}
  </div>
{all_baselines_html}
</section>

<!-- ═══════════════════════════════════════════════════════
     SECTION 3 — EVIDENCE MAP
     ═══════════════════════════════════════════════════════ -->
<section class="map-section rise" id="map" style="--i:3">
  <div class="section-heading">Evidence map: every module, every function</div>

  <div class="toolbar" role="group" aria-label="Filter the evidence map">
    <label class="sr-only" for="fl-search">Filter modules by name</label>
    <input id="fl-search" class="toolbar__search" type="search"
           placeholder="filter modules by name" autocomplete="off">
    <button type="button" class="chip" data-only="never" aria-pressed="false">
      has unobserved
    </button>
    <button type="button" class="chip" data-only="clean" aria-pressed="false">
      fully observed
    </button>
    <button type="button" class="chip" data-only="product" aria-pressed="false">
      product scope only
    </button>
    <span class="toolbar__status" id="fl-status" role="status" aria-live="polite"></span>
  </div>
  <div class="legend">
    <div class="legend__item">
      <span class="swatch swatch--insitu"></span>
      observed in situ
    </div>
    {driver_legend_item}
    {redundant_legend_item}
    <div class="legend__item">
      <span class="swatch swatch--never"></span>
      never observed
    </div>
  </div>
  <div class="map">
<div class="map-body">
{map_html}
</div>
  <p class="map-hint">Every square is one function. Hover a square to see which.
     The map scrolls; the rest of the report continues below.</p>
  </div>
</section>

<hr class="sep">

<!-- ═══════════════════════════════════════════════════════
     SECTION 4 — DRIVER RESULTS
     ═══════════════════════════════════════════════════════ -->
<section class="drivers-section rise" style="--i:2">
  <div class="section-heading" id="drivers">Driver results: {e(str(len(confirmed_drivers)))} confirmed, {e(str(len(redundant_drivers)))} driver redundant, {e(str(len(attempted_drivers)))} not confirmed</div>
{drivers_html}
{f'<div style="margin-top:28px"><div class="section-heading" style="color:#b8a030;">Driver made redundant by baseline ({e(str(len(redundant_drivers)))})</div><p style="font-size:12px;color:var(--muted);margin-bottom:12px;">These functions were genuinely observed by a baseline, making the corresponding driver redundant. The unit&#x2019;s provenance is observed_in_situ. The driver file is kept as a record of the path that was built.</p>' + redundant_html + '</div>' if redundant_drivers else ''}
{f'<div style="margin-top:20px"><div class="section-heading" style="color:#cc4444;">Attempted, not confirmed ({e(str(len(attempted_drivers)))})</div>' + attempted_html + '</div>' if attempted_drivers else ''}
</section>

{'<hr class="sep">' if refusal_rows else ''}

<!-- ═══════════════════════════════════════════════════════
     SECTION 5 — REFUSAL DISTRIBUTION
     ═══════════════════════════════════════════════════════ -->
{_refusal_section_html}

<footer class="footer">
  Made with IBM Bob : First Light v{e(version)} : {e(generated_at)}
</footer>

<script>
{_HTML_JS}
</script>

</body>
</html>"""

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_doc, encoding="utf-8")
    print(f"[first_light] HTML report written to {out_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def pct(n: int, total: int) -> str:
    if total == 0:
        return "  n/a"
    return f"{100 * n / total:5.1f}%"


def render_report(
    all_funcs: list[FuncInfo],
    all_obs: list[FuncInfo],
    all_never: list[FuncInfo],
    prod_funcs: list[FuncInfo],
    prod_obs: list[FuncInfo],
    prod_never: list[FuncInfo],
    breakdown: dict[str, dict],
    prod_breakdown: dict[str, dict],
    exclude_dirs: set[str],
    baselines: "list[BaselineInfo] | None" = None,
) -> str:
    lines = []
    w = lines.append

    w("=" * 70)
    w("  FIRST LIGHT — Function Observation Report")
    w("=" * 70)

    # ── whole-package summary ─────────────────────────────────────────────
    w("")
    w("  WHOLE PACKAGE")
    w(f"  {'Total functions':<28} {len(all_funcs):>6}")
    w(f"  {'Observed (body executed)':<28} {len(all_obs):>6}  {pct(len(all_obs), len(all_funcs))}")
    w(f"  {'Never observed':<28} {len(all_never):>6}  {pct(len(all_never), len(all_funcs))}")

    # ── product-code summary ──────────────────────────────────────────────
    if exclude_dirs:
        w("")
        w(f"  PRODUCT CODE  (excluding: {', '.join(sorted(exclude_dirs))})")
        w(f"  {'Total functions':<28} {len(prod_funcs):>6}")
        w(f"  {'Observed (body executed)':<28} {len(prod_obs):>6}  {pct(len(prod_obs), len(prod_funcs))}")
        w(f"  {'Never observed':<28} {len(prod_never):>6}  {pct(len(prod_never), len(prod_funcs))}")

    # ── per-baseline breakdown (product scope) ────────────────────────────
    if baselines and exclude_dirs:
        w("")
        w("  PER-BASELINE BREAKDOWN  (product code)")
        w(f"  {'Baseline':<18} {'RC':>4}  {'Observed':>8}  {'Unique add':>10}  {'Partial?':>8}")
        w("  " + "-" * 54)

        # Compute per-baseline observed counts over the product functions.
        bl_obs_sets: dict[str, set[int]] = {}
        for bl in baselines:
            obs_fns, _ = classify_functions(prod_funcs, bl.executed)
            bl_obs_sets[bl.id] = {id(fn) for fn in obs_fns}

        seen_ids: set[int] = set()
        for bl in baselines:
            obs_ids = bl_obs_sets[bl.id]
            unique_add = len(obs_ids - seen_ids)
            seen_ids |= obs_ids
            partial = "yes" if bl.exit_code not in (0, 1) else ("FAIL(tests)" if bl.exit_code == 1 else "no")
            w(f"  {bl.id:<18} {bl.exit_code:>4}  {len(obs_ids):>8}  {unique_add:>10}  {partial:>8}")

    # ── whole-package breakdown ───────────────────────────────────────────
    w("")
    w("  BREAKDOWN BY DIRECTORY (whole package)")
    w(f"  {'Directory':<30} {'Total':>6}  {'Observed':>8}  {'Never':>8}  {'Obs%':>6}")
    w("  " + "-" * 66)
    for label, d in breakdown.items():
        w(f"  {label:<30} {d['total']:>6}  {d['observed']:>8}  {d['never']:>8}  {pct(d['observed'], d['total'])}")

    # ── product-code breakdown ────────────────────────────────────────────
    if exclude_dirs and prod_breakdown:
        w("")
        w("  BREAKDOWN BY DIRECTORY (product code only)")
        w(f"  {'Directory':<30} {'Total':>6}  {'Observed':>8}  {'Never':>8}  {'Obs%':>6}")
        w("  " + "-" * 66)
        for label, d in prod_breakdown.items():
            w(f"  {label:<30} {d['total']:>6}  {d['observed']:>8}  {d['never']:>8}  {pct(d['observed'], d['total'])}")

    w("")
    w("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --promote-driver
# ---------------------------------------------------------------------------

def _is_not_a_call_site(line_text: str, name: str) -> str | None:
    """Return a refusal reason if *line_text* cannot be a call site for *name*.

    The name appearing on a line is necessary but nowhere near sufficient. A
    definition, an import, an ``__all__`` entry or a comment all contain the
    name and none of them is a place the function is reached in production.
    Accepting them would let a driver satisfy the check without citing a use.
    """
    stripped = line_text.strip()
    if stripped.startswith("#"):
        return "the cited line is a comment"
    if _re_mod.match(r"^\s*(async\s+)?def\s+" + _re_mod.escape(name) + r"\b", line_text):
        return "the cited line is the function's own definition"
    if _re_mod.match(r"^\s*@", line_text):
        return "the cited line is a decorator"
    if _re_mod.match(r"^\s*(from|import)\s", line_text):
        return "the cited line is an import"
    if _re_mod.match(r"^\s*__all__\s*=", line_text):
        return "the cited line is an __all__ declaration"
    return None


def _run_driver_under_coverage(
    driver_abs: str,
    src_abs: str,
    pkg_abs: str,
    body_start: int,
    body_end: int,
    python: str,
    timeout: int,
) -> tuple[str, list[int]]:
    """Run *driver_abs* under coverage and check body lines in *src_abs*.

    Returns (status, confirmed_lines) where status is one of:
      "reached"  — at least one body line was executed
      "not_reached" — driver ran cleanly but zero body lines hit
      "crash"    — driver process returned a non-0/1 exit code
      "cov_fail" — coverage JSON export failed
    """
    body_lines = set(range(body_start, body_end + 1))

    with tempfile.TemporaryDirectory(prefix="fl_promo_") as td:
        data_file = os.path.join(td, ".coverage")
        json_out  = os.path.join(td, "cov.json")

        run_result = subprocess.run(
            [python, "-m", "coverage", "run",
             f"--source={pkg_abs}",
             f"--data-file={data_file}",
             driver_abs],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(Path(".").resolve()),
        )

        if run_result.returncode != 0:
            return "crash", []

        exp_result = subprocess.run(
            [python, "-m", "coverage", "json",
             f"--data-file={data_file}",
             "-o", json_out],
            capture_output=True, text=True, timeout=30,
        )
        if exp_result.returncode != 0:
            return "cov_fail", []

        with open(json_out) as fh:
            cov = json.load(fh)

        for file_path, file_data in cov.get("files", {}).items():
            if Path(file_path).resolve() == Path(src_abs).resolve():
                executed = set(file_data.get("executed_lines", []))
                hit = sorted(executed & body_lines)
                if hit:
                    return "reached", hit
                break

    return "not_reached", []


def cmd_promote_driver(
    driver_paths: list[Path],
    evidence_path: Path,
    python: str,
    pkg_abs: str,
    timeout: int,
) -> int:
    """Promote one or more drivers into evidence.json with live coverage confirmation.

    For each driver:
      1. Resolve the target unit from evidence.json by filename→qualified-name.
      2. Run the driver under coverage with *timeout* seconds.
      3. Confirm that lines inside the unit's real body range were executed.
      4. On success, record observed_under_driver + confirmed lines + driver path
         + call site into the in-memory document.
      5. On failure, leave the unit as never_observed and report the reason.

    The evidence file is read once before the loop, all mutations are accumulated
    in memory, and a single atomic os.replace() write happens at the end.

    Returns 0 if every driver succeeded, 1 if any failed.
    """
    promoted        = 0
    redundant_count = 0
    failed          = 0
    touched         = 0

    # ── read evidence once ─────────────────────────────────────────────────
    with open(evidence_path, encoding="utf-8") as fh:
        doc = json.load(fh)

    for driver_path in driver_paths:
        driver_abs = str(driver_path.resolve())
        print(f"[promote-driver] processing {driver_path.name} …", file=sys.stderr)

        # ── resolve unit ──────────────────────────────────────────────────
        # Re-use the already-loaded doc so we see mutations from earlier
        # iterations in the same run (e.g. same unit promoted twice).
        qname_suffix = driver_path.stem
        unit_key = None
        unit = None
        for key, u in doc.get("units", {}).items():
            if "::" not in key:
                continue
            qname_part = key.split("::", 1)[1]
            if "#" in qname_part:
                qname_part = qname_part.rsplit("#", 1)[0]
            if qname_part == qname_suffix:
                unit_key = key
                unit = u
                break
        if unit_key is None:
            _reason = (
                f"no unit found for qualified name '{driver_path.stem}' in {evidence_path}"
            )
            print(
                f"[promote-driver] FAIL  {driver_path.name} — {_reason}",
                file=sys.stderr,
            )
            # No unit to annotate — nothing to write into doc.
            failed += 1
            continue

        # ── handle already-promoted or redundant ──────────────────────────
        current_prov = unit.get("provenance")
        if current_prov == PROVENANCE_IN_SITU and not unit.get("driver_redundant_baseline"):
            # The unit is now reached by a baseline: the driver is redundant.
            # Keep provenance=observed_in_situ (the observation is genuine).
            # Record the driver metadata under driver_redundant_baseline.
            bl_ids_that_cover = unit.get("observed_in_baseline", [])
            redundant_by = bl_ids_that_cover[0] if bl_ids_that_cover else "unknown"
            # Extract call site from driver even though we won't do a full
            # coverage run — we still want to store the driver metadata.
            call_site_text, _ = _driver_call_site(driver_path)
            doc["units"][unit_key]["driver"]                    = str(driver_path.resolve())
            doc["units"][unit_key]["call_site"]                 = call_site_text
            doc["units"][unit_key]["driver_redundant_baseline"] = redundant_by
            # provenance stays PROVENANCE_IN_SITU — do NOT change it.
            print(
                f"[promote-driver] REDUNDANT  {driver_path.name} — "
                f"unit already reached by baseline '{redundant_by}'; driver is redundant",
                file=sys.stderr,
            )
            redundant_count += 1
            continue
        if current_prov == PROVENANCE_IN_SITU and unit.get("driver_redundant_baseline"):
            # Already recorded as redundant; skip silently.
            print(
                f"[promote-driver] SKIP  {driver_path.name} — already recorded as redundant",
                file=sys.stderr,
            )
            continue
        if current_prov == PROVENANCE_UNDER_DRIVER:
            print(
                f"[promote-driver] SKIP  {driver_path.name} — already observed_under_driver",
                file=sys.stderr,
            )
            continue
        if current_prov == PROVENANCE_SUPERSEDED:
            # Legacy: migrate on-the-fly to observed_in_situ + driver_redundant_baseline.
            bl_ids_that_cover = unit.get("observed_in_baseline", [])
            redundant_by = unit.get("superseded_by") or (bl_ids_that_cover[0] if bl_ids_that_cover else "unknown")
            call_site_text, _ = _driver_call_site(driver_path)
            doc["units"][unit_key]["provenance"]                = PROVENANCE_IN_SITU
            doc["units"][unit_key]["driver"]                    = str(driver_path.resolve())
            doc["units"][unit_key]["call_site"]                 = call_site_text
            doc["units"][unit_key]["driver_redundant_baseline"] = redundant_by
            doc["units"][unit_key].pop("superseded_by", None)
            print(
                f"[promote-driver] REDUNDANT(migrated)  {driver_path.name} — "
                f"legacy superseded entry migrated to observed_in_situ + driver_redundant_baseline",
                file=sys.stderr,
            )
            redundant_count += 1
            continue

        src_abs    = unit["file"]
        body_start = unit["body_start"]
        body_end   = unit["body_end"]

        # ── run under coverage ────────────────────────────────────────────
        try:
            status, confirmed_lines = _run_driver_under_coverage(
                driver_abs=driver_abs,
                src_abs=src_abs,
                pkg_abs=pkg_abs,
                body_start=body_start,
                body_end=body_end,
                python=python,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(
                f"[promote-driver] FAIL  {driver_path.name} — timed out after {timeout}s",
                file=sys.stderr,
            )
            failed += 1
            continue

        if status != "reached":
            _rc_map = {
                "not_reached": (RefusalClass.body_never_reached,
                                f"driver ran but zero body lines ({body_start}-{body_end}) hit in {src_abs}"),
                "crash":       (RefusalClass.driver_exited_nonzero,
                                "driver process exited with non-zero return code"),
                "cov_fail":    (RefusalClass.coverage_export_failed,
                                "coverage JSON export failed"),
            }
            _rc, reason = _rc_map.get(status, (RefusalClass.body_never_reached, status))
            print(
                f"[promote-driver] FAIL  {driver_path.name} — {reason}",
                file=sys.stderr,
            )
            doc["units"][unit_key]["refusal_class"]  = _rc.value
            doc["units"][unit_key]["refusal_reason"] = reason
            touched += 1
            failed += 1
            continue

        # ── extract call site from driver comment ─────────────────────────
        call_site, is_indirect = _driver_call_site(driver_path)

        # Coverage has confirmed the driver executed lines inside the real
        # function. Record that now, before the call site is checked.
        # Whether the driver REACHED the function and whether it JUSTIFIED its
        # claim are two different facts. A refused promotion must not erase the
        # first one: the honest record is that the code ran, and that the claim
        # about where it runs in production could not be verified.
        doc["units"][unit_key]["driver_reached"]       = True
        doc["units"][unit_key]["driver_reached_lines"] = confirmed_lines
        doc["units"][unit_key]["driver_attempted"]     = driver_abs
        doc["units"][unit_key]["driver_declared_call_site"] = call_site
        touched += 1

        # ── validate call site ────────────────────────────────────────────
        # Direct call site rules (same as before):
        #   1. The file named before the colon must exist on disk.
        #   2. The target function's simple name must appear on that line.
        #   3. The line must NOT fall inside the target function's own body.
        #
        # Indirect call site format (two semicolon-separated segments):
        #   "file:N -- dispatch_line ; file:M -- binding_name"
        #   Segment 1: the dispatch line (indirect call; the function's name
        #              does NOT need to appear here — only the file:line is
        #              checked for existence).
        #   Segment 2: the binding line (where the function is bound by name).
        #              The function's simple name MUST appear on this line.
        #              Rule 3 (not-inside-own-body) applies to both lines.
        import re as _re

        # A driver with no "# call site:" comment declares nothing, so there is
        # nothing to verify.  Promoting it would record the function as reached
        # in production on the strength of an assertion nobody made.  An absent
        # claim is not a verified one: the gate fails closed.
        if not call_site:
            _reason = (
                "no '# call site:' comment; a driver must declare where the "
                "function is reached in production so the claim can be checked"
            )
            print(
                f"[promote-driver] FAIL  {driver_path.name} -- {_reason}"
            )
            doc["units"][unit_key]["refusal_class"]  = RefusalClass.no_call_site.value
            doc["units"][unit_key]["refusal_reason"] = _reason
            failed += 1
            continue

        if call_site:
            if is_indirect:
                # ── indirect call site: two segments separated by " ; " ──
                segments = [s.strip() for s in call_site.split(";")]
                if len(segments) != 2:
                    _reason = (
                        "indirect call site must have exactly two segments separated by ';': "
                        "'dispatch_file:N -- text ; binding_file:M -- binding_name'. "
                        "Correct the '# call site (indirect):' comment."
                    )
                    print(
                        f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                        file=sys.stderr,
                    )
                    doc["units"][unit_key]["refusal_class"]  = RefusalClass.indirect_wrong_format.value
                    doc["units"][unit_key]["refusal_reason"] = _reason
                    failed += 1
                    continue

                dispatch_seg, binding_seg = segments

                # Validate both segments: file exists, line in range.
                # For the binding segment, also check that the function's
                # simple name appears on that line.
                func_simple_name = unit_key.split("::", 1)[1].rsplit(".", 1)[-1] if "::" in unit_key else unit_key
                if "#" in func_simple_name:
                    func_simple_name = func_simple_name.rsplit("#", 1)[0]

                cs_ok = True
                _cs_refusal_class: RefusalClass | None = None
                _cs_refusal_reason: str = ""
                for seg_label, seg_text, check_name in [
                    ("dispatch", dispatch_seg, False),
                    ("binding",  binding_seg,  True),
                ]:
                    seg_match = _re.match(r"^(.+):(\d+)", seg_text)
                    if not seg_match:
                        _cs_refusal_reason = (
                            f"indirect call site {seg_label} segment '{seg_text}' "
                            f"does not match 'file:N -- ...' format."
                        )
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- {_cs_refusal_reason}",
                            file=sys.stderr,
                        )
                        _cs_refusal_class = RefusalClass.call_site_not_file_line
                        cs_ok = False
                        break
                    seg_file_raw = seg_match.group(1).strip()
                    seg_line = int(seg_match.group(2))
                    seg_file_path = Path(seg_file_raw)
                    if not seg_file_path.is_absolute():
                        seg_file_path = Path(".").resolve() / seg_file_raw
                    if not seg_file_path.is_file():
                        _cs_refusal_reason = (
                            f"indirect call site {seg_label} file '{seg_file_raw}' does not exist."
                        )
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- {_cs_refusal_reason}",
                            file=sys.stderr,
                        )
                        _cs_refusal_class = RefusalClass.call_site_file_not_found
                        cs_ok = False
                        break
                    # Same containment rule as the direct form. Without it the
                    # indirect form is a way around it: a driver could cite a
                    # throwaway file anywhere on disk, or itself, as the place
                    # the function is reached in production.
                    try:
                        _seg_inside = seg_file_path.resolve().is_relative_to(Path(pkg_abs).resolve())
                    except (OSError, ValueError):
                        _seg_inside = False
                    if not _seg_inside:
                        _cs_refusal_reason = (
                            f"indirect call site {seg_label} file '{seg_file_raw}' is "
                            f"outside the package under analysis."
                        )
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- {_cs_refusal_reason}",
                            file=sys.stderr,
                        )
                        _cs_refusal_class = RefusalClass.call_site_outside_package
                        cs_ok = False
                        break
                    try:
                        seg_text_lines = seg_file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        if seg_line < 1 or seg_line > len(seg_text_lines):
                            _cs_refusal_reason = (
                                f"indirect call site {seg_label} line {seg_line} is out of range "
                                f"for '{seg_file_raw}' ({len(seg_text_lines)} lines)."
                            )
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- {_cs_refusal_reason}",
                                file=sys.stderr,
                            )
                            _cs_refusal_class = RefusalClass.call_site_line_out_of_range
                            cs_ok = False
                            break
                        seg_line_text = seg_text_lines[seg_line - 1]
                        if check_name and func_simple_name not in seg_line_text:
                            _cs_refusal_reason = (
                                f"function name '{func_simple_name}' not found on "
                                f"{seg_label} line {seg_line} of '{seg_file_raw}': "
                                f"{seg_line_text.strip()!r}. "
                                f"Correct the '# call site (indirect):' comment."
                            )
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- {_cs_refusal_reason}",
                                file=sys.stderr,
                            )
                            _cs_refusal_class = RefusalClass.name_not_on_line
                            cs_ok = False
                            break
                        # Rule 2b: the name being present is not enough.  A
                        # definition, an import or an __all__ entry all contain
                        # it and none is a place the function is reached.
                        if check_name:
                            _bad = _is_not_a_call_site(seg_line_text, func_simple_name)
                            if _bad:
                                _cs_refusal_reason = (
                                    f"{seg_label} line {seg_line} of '{seg_file_raw}' "
                                    f"cannot be a call site: {_bad}."
                                )
                                print(
                                    f"[promote-driver] FAIL  {driver_path.name} -- {_cs_refusal_reason}",
                                    file=sys.stderr,
                                )
                                _cs_refusal_class = RefusalClass.line_not_a_call_site
                                cs_ok = False
                                break
                        # Rule 3: neither line may fall inside the function's own body.
                        if Path(seg_file_raw).resolve() == Path(src_abs).resolve() and body_start <= seg_line <= body_end:
                            _cs_refusal_reason = (
                                f"indirect call site {seg_label} line {seg_line} falls inside "
                                f"the target function's own body range ({body_start}-{body_end})."
                            )
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- {_cs_refusal_reason}",
                                file=sys.stderr,
                            )
                            _cs_refusal_class = RefusalClass.call_site_inside_own_body
                            cs_ok = False
                            break
                    except OSError as _cs_err:
                        _cs_refusal_reason = (
                            f"could not read indirect call site {seg_label} file "
                            f"'{seg_file_raw}': {_cs_err}"
                        )
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- {_cs_refusal_reason}",
                            file=sys.stderr,
                        )
                        _cs_refusal_class = RefusalClass.call_site_file_not_found
                        cs_ok = False
                        break

                if not cs_ok:
                    if _cs_refusal_class is not None:
                        doc["units"][unit_key]["refusal_class"]  = _cs_refusal_class.value
                        doc["units"][unit_key]["refusal_reason"] = _cs_refusal_reason
                    failed += 1
                    continue

            else:
                # ── direct call site: single "file:N -- name(...)" ────────
                _cs_file_match = _re.match(r"^(.+):(\d+)", call_site)
                if _cs_file_match:
                    cs_file_raw = _cs_file_match.group(1).strip()
                    cs_line = int(_cs_file_match.group(2))

                    cs_file_path = Path(cs_file_raw)
                    if not cs_file_path.is_absolute():
                        cs_file_path = Path(".").resolve() / cs_file_raw

                    # Rule 1: the file must exist.
                    if not cs_file_path.is_file():
                        _reason = (
                            f"call site file '{cs_file_raw}' does not exist; "
                            f"correct the '# call site:' comment in the driver file."
                        )
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                            file=sys.stderr,
                        )
                        doc["units"][unit_key]["refusal_class"]  = RefusalClass.call_site_file_not_found.value
                        doc["units"][unit_key]["refusal_reason"] = _reason
                        failed += 1
                        continue

                    # Rule 1b: the call site must sit inside the package under analysis.
                    # Without this a driver can name itself, or any file on disk, as the
                    # place the function is reached in production. A file outside the
                    # measured package is not a production call site.
                    try:
                        _inside = cs_file_path.resolve().is_relative_to(Path(pkg_abs).resolve())
                    except (OSError, ValueError):
                        _inside = False
                    if not _inside:
                        _reason = (
                            f"call site '{cs_file_raw}' is outside the package under "
                            f"analysis; a driver cannot cite itself or an unmeasured "
                            f"file as where the function is reached in production."
                        )
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                            file=sys.stderr,
                        )
                        doc["units"][unit_key]["refusal_class"]  = RefusalClass.call_site_outside_package.value
                        doc["units"][unit_key]["refusal_reason"] = _reason
                        failed += 1
                        continue

                    # Rule 2: the target function's simple name must appear on that line.
                    func_simple_name = unit_key.split("::", 1)[1].rsplit(".", 1)[-1] if "::" in unit_key else unit_key
                    if "#" in func_simple_name:
                        func_simple_name = func_simple_name.rsplit("#", 1)[0]
                    try:
                        cs_lines = cs_file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        if cs_line < 1 or cs_line > len(cs_lines):
                            _reason = (
                                f"call site line {cs_line} is out of range for "
                                f"'{cs_file_raw}' ({len(cs_lines)} lines); "
                                f"correct the '# call site:' comment."
                            )
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                                file=sys.stderr,
                            )
                            doc["units"][unit_key]["refusal_class"]  = RefusalClass.call_site_line_out_of_range.value
                            doc["units"][unit_key]["refusal_reason"] = _reason
                            failed += 1
                            continue
                        cs_text = cs_lines[cs_line - 1]
                        if func_simple_name not in cs_text:
                            _reason = (
                                f"function name '{func_simple_name}' not found on "
                                f"line {cs_line} of '{cs_file_raw}'; "
                                f"correct the '# call site:' comment in the driver file."
                            )
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                                file=sys.stderr,
                            )
                            doc["units"][unit_key]["refusal_class"]  = RefusalClass.name_not_on_line.value
                            doc["units"][unit_key]["refusal_reason"] = _reason
                            failed += 1
                            continue
                        _bad = _is_not_a_call_site(cs_text, func_simple_name)
                        if _bad:
                            _reason = (
                                f"line {cs_line} of '{cs_file_raw}' cannot be a "
                                f"call site: {_bad}."
                            )
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                                file=sys.stderr,
                            )
                            doc["units"][unit_key]["refusal_class"]  = RefusalClass.line_not_a_call_site.value
                            doc["units"][unit_key]["refusal_reason"] = _reason
                            failed += 1
                            continue
                    except OSError as _cs_err:
                        _reason = f"could not read call site file '{cs_file_raw}': {_cs_err}"
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                            file=sys.stderr,
                        )
                        doc["units"][unit_key]["refusal_class"]  = RefusalClass.call_site_file_not_found.value
                        doc["units"][unit_key]["refusal_reason"] = _reason
                        failed += 1
                        continue

                    # Rule 3: call site must not fall inside the target's own body.
                    if Path(cs_file_raw).resolve() == Path(src_abs).resolve() and body_start <= cs_line <= body_end:
                        _reason = (
                            f"call site line {cs_line} falls inside the target function's "
                            f"own body range ({body_start}-{body_end}); a function cannot "
                            f"be its own caller.  Correct the '# call site:' comment "
                            f"in the driver file."
                        )
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                            file=sys.stderr,
                        )
                        doc["units"][unit_key]["refusal_class"]  = RefusalClass.call_site_inside_own_body.value
                        doc["units"][unit_key]["refusal_reason"] = _reason
                        failed += 1
                        continue
                else:
                    # A call site that does not parse as 'file:line' states nothing a
                    # checker can open and confirm. Prose is not a citation: accepting
                    # it would record the function as reached in production on an
                    # assertion nobody can check, which is the failure this gate exists
                    # to prevent.
                    _reason = (
                        f"call site {call_site!r} is not in 'file:line' form, so there "
                        f"is nothing to open and confirm."
                    )
                    print(
                        f"[promote-driver] FAIL  {driver_path.name} -- {_reason}",
                        file=sys.stderr,
                    )
                    doc["units"][unit_key]["refusal_class"]  = RefusalClass.call_site_not_file_line.value
                    doc["units"][unit_key]["refusal_reason"] = _reason
                    failed += 1
                    continue


        # ── record promotion in the in-memory document ────────────────────
        doc["units"][unit_key]["provenance"]               = PROVENANCE_UNDER_DRIVER
        doc["units"][unit_key]["driver"]                   = driver_abs
        doc["units"][unit_key]["call_site"]                = call_site
        doc["units"][unit_key]["coverage_confirmed_lines"] = confirmed_lines

        print(
            f"[promote-driver] OK    {driver_path.name} — "
            f"lines {confirmed_lines[:4]}{'...' if len(confirmed_lines) > 4 else ''}",
            file=sys.stderr,
        )
        promoted += 1

    # ── write evidence.json atomically ────────────────────────────────────
    # Write to a temp file adjacent to the target, then rename so readers
    # never see a partially-written file.
    # Write whenever any mutation was made (full promotions OR superseded markings).
    # Write when anything changed, including a refusal that still recorded
    # that the driver reached the function.
    if promoted > 0 or redundant_count > 0 or touched > 0:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=evidence_path.parent,
            prefix=".evidence_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
            os.replace(tmp_path, str(evidence_path))
        except Exception:
            # Clean up the temp file if the rename fails.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ── summary ───────────────────────────────────────────────────────────
    total = promoted + redundant_count + failed
    print(
        f"\n[promote-driver] {promoted}/{total} promoted, "
        f"{redundant_count} redundant (driver made obsolete by baseline), "
        f"{failed} failed",
        file=sys.stderr,
    )
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# --refusal-report
# ---------------------------------------------------------------------------

def cmd_refusal_report(
    drivers_dir: Path,
    evidence_path: Path,
    python: str,
    pkg_abs: str,
    timeout: int,
) -> int:
    """Run every driver against a scratch copy of evidence.json and print the
    refusal distribution.  The real evidence file is never written.

    Returns 0 always (this is a reporting command, not a gate).
    """
    if not drivers_dir.is_dir():
        print(f"[refusal-report] ERROR: drivers directory not found: {drivers_dir}", file=sys.stderr)
        return 1

    driver_paths = sorted(drivers_dir.glob("*.py"))
    if not driver_paths:
        print("[refusal-report] no driver files found in", drivers_dir, file=sys.stderr)
        return 0

    # Deep-copy the evidence document so the real file is never touched.
    with open(evidence_path, encoding="utf-8") as fh:
        doc_original = json.load(fh)
    doc_scratch = copy.deepcopy(doc_original)

    # Write the scratch copy to a temp file so cmd_promote_driver can use its
    # normal read/write path without touching the real file.
    import tempfile as _tmp
    tmp_fd, tmp_path = _tmp.mkstemp(suffix=".json", prefix="fl_refusal_")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(doc_scratch, fh, indent=2)

        scratch_path = Path(tmp_path)

        # Run the full promote logic against the scratch file.
        # cmd_promote_driver writes its results back into scratch_path via
        # os.replace — the real evidence_path is untouched.
        cmd_promote_driver(
            driver_paths=driver_paths,
            evidence_path=scratch_path,
            python=python,
            pkg_abs=pkg_abs,
            timeout=timeout,
        )

        # Read back the annotated scratch document.
        with open(scratch_path, encoding="utf-8") as fh:
            doc_after = json.load(fh)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # ── tally outcomes ────────────────────────────────────────────────────
    attempted   = len(driver_paths)
    promoted    = 0
    redundant   = 0
    refused_by_class: dict[str, int] = {}

    for driver_path in driver_paths:
        stem = driver_path.stem
        # Find the matching unit in the annotated document.
        unit = None
        for key, u in doc_after.get("units", {}).items():
            if "::" not in key:
                continue
            qname_part = key.split("::", 1)[1]
            if "#" in qname_part:
                qname_part = qname_part.rsplit("#", 1)[0]
            if qname_part == stem:
                unit = u
                break

        if unit is None:
            # unit_not_found — driver stem has no matching unit key
            rc = RefusalClass.unit_not_found.value
            refused_by_class[rc] = refused_by_class.get(rc, 0) + 1
            continue

        prov = unit.get("provenance", PROVENANCE_NEVER)
        if prov == PROVENANCE_UNDER_DRIVER:
            promoted += 1
        elif prov == PROVENANCE_IN_SITU and unit.get("driver_redundant_baseline"):
            redundant += 1
        else:
            # Refused.  Read back the refusal_class that cmd_promote_driver wrote.
            rc = unit.get("refusal_class", "unknown")
            refused_by_class[rc] = refused_by_class.get(rc, 0) + 1

    total_refused = sum(refused_by_class.values())

    # ── print the distribution ────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  REFUSAL REPORT — driver evaluation distribution")
    print("=" * 60)
    print(f"  Drivers measured : {attempted}")
    print(f"  Promoted         : {promoted}")
    print(f"  Made redundant   : {redundant}  (unit reached by a baseline)")
    print(f"  Refused          : {total_refused}")
    if refused_by_class:
        print()
        print(f"  {'Refusal class':<36} {'Count':>5}")
        print("  " + "-" * 44)
        for rc_val, count in sorted(refused_by_class.items(), key=lambda kv: -kv[1]):
            print(f"  {rc_val:<36} {count:>5}")
    print("=" * 60)
    print()
    print("  NOTE: this report ran drivers against a scratch copy of")
    print("  evidence.json.  The published evidence file was not changed.")
    print()
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="First Light — observe which Python functions execute.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--package", default=None,
        help="Path to the Python package directory to analyse (e.g. target/visidata/visidata). "
             "Required unless --report is given.",
    )
    parser.add_argument(
        "--runner", dest="runners", action="append", default=None, metavar="SCRIPT",
        help="Python script that runs the target application. "
             "Pass multiple times to collect several baselines in one run. "
             "Required unless --report is given.",
    )
    parser.add_argument(
        "--runner-id", dest="runner_ids", action="append", default=None, metavar="ID",
        help="Short identifier for the corresponding --runner (e.g. 'cli', 'test_suite'). "
             "Must appear in the same order as --runner. "
             "Defaults to 'baseline_0', 'baseline_1', … when omitted.",
    )
    parser.add_argument(
        "--python", default=sys.executable,
        help="Python interpreter to use (default: same as this script)",
    )
    parser.add_argument(
        "--exclude", nargs="*", default=["tests", "vendor", "apps", "experimental"],
        metavar="DIR",
        help="Directory names to exclude from the product-code figure "
             "(default: tests vendor apps experimental)",
    )
    parser.add_argument(
        "--rcfile", default=None,
        help="Optional coverage.py rc file",
    )
    parser.add_argument(
        "--json", dest="json_out", default=None, metavar="PATH",
        help="Also write results as JSON to PATH",
    )
    parser.add_argument(
        "--evidence", dest="evidence_out", default=None, metavar="PATH",
        help="Write evidence.json artifact to PATH "
             "(recommended: <target-repo-root>/evidence.json)",
    )
    parser.add_argument(
        "--report", dest="report_out", default=None, metavar="PATH",
        help="Read an existing evidence.json and write a self-contained HTML report to PATH. "
             "When this flag is given, --package and --runner are optional (no coverage run is performed).",
    )

    # ── --promote-driver ──────────────────────────────────────────────────
    promote_group = parser.add_argument_group(
        "--promote-driver options",
        "Run drivers under coverage and promote confirmed units in evidence.json.",
    )
    promote_group.add_argument(
        "--promote-driver", dest="promote_driver", nargs="*", metavar="DRIVER_PATH",
        help="Driver script(s) to promote, or omit paths and pass --all to promote every "
             "file in drivers/. Requires --evidence pointing at the evidence.json to update.",
    )
    promote_group.add_argument(
        "--all", dest="promote_all", action="store_true",
        help="Promote every driver in the drivers/ directory (use with --promote-driver).",
    )
    promote_group.add_argument(
        "--drivers-dir", dest="drivers_dir",
        default=str(Path(__file__).parent / "drivers"),
        metavar="DIR",
        help="Directory to scan when --all is given (default: drivers/ next to this script).",
    )
    promote_group.add_argument(
        "--pkg", dest="pkg_for_driver",
        default=None, metavar="PKG_PATH",
        help="Package path for --promote-driver coverage run. "
             "Defaults to the package recorded in evidence.json.",
    )
    promote_group.add_argument(
        "--timeout", dest="driver_timeout", type=int, default=30, metavar="SECONDS",
        help="Per-driver subprocess timeout in seconds (default: 30).",
    )

    # ── --refusal-report ──────────────────────────────────────────────────
    parser.add_argument(
        "--refusal-report", dest="refusal_report", action="store_true",
        help=(
            "Run every driver in --drivers-dir against a COPY of the evidence "
            "file and print the refusal distribution.  The real evidence file is "
            "never modified.  Requires --evidence pointing at the evidence.json to "
            "read, and the package path recorded in that file must still be valid.  "
            "Use --pkg to override the package path."
        ),
    )

    args = parser.parse_args(argv)

    # ── --promote-driver fast-path ────────────────────────────────────────
    if args.promote_driver is not None or args.promote_all:
        # Require --evidence so we know which file to update.
        evidence_path_str = args.evidence_out or str(Path(__file__).parent / "evidence.json")
        evidence_path = Path(evidence_path_str)
        if not evidence_path.is_file():
            print(
                f"[promote-driver] ERROR: evidence file not found: {evidence_path}\n"
                f"  Pass --evidence <path> to specify the evidence.json to update.",
                file=sys.stderr,
            )
            return 1

        # Collect driver paths.
        if args.promote_all:
            drivers_dir = Path(args.drivers_dir)
            if not drivers_dir.is_dir():
                print(f"[promote-driver] ERROR: drivers directory not found: {drivers_dir}", file=sys.stderr)
                return 1
            driver_paths = sorted(drivers_dir.glob("*.py"))
        else:
            driver_paths = [Path(p) for p in (args.promote_driver or [])]

        if not driver_paths:
            print("[promote-driver] ERROR: no drivers specified. Pass paths or use --all.", file=sys.stderr)
            return 1

        # Resolve pkg_abs: explicit --pkg takes priority, else read from evidence.json.
        if args.pkg_for_driver:
            pkg_abs = str(Path(args.pkg_for_driver).resolve())
        else:
            with open(evidence_path, encoding="utf-8") as fh:
                _ev = json.load(fh)
            # Support both old single-baseline schema (v0.1) and new baselines list (v0.2+).
            _bl0 = (_ev.get("baselines") or [{}])[0] if _ev.get("baselines") else _ev.get("baseline", {})
            pkg_abs = _bl0.get("package", "")
            if not pkg_abs:
                print(
                    "[promote-driver] ERROR: cannot determine package path. "
                    "Pass --pkg <path> or ensure evidence.json has baselines[0].package set.",
                    file=sys.stderr,
                )
                return 1

        # Resolve python interpreter: --python takes priority, else read from evidence.json.
        python_interp = args.python
        if python_interp == sys.executable:
            # user did not explicitly pass --python; check evidence first baseline
            with open(evidence_path, encoding="utf-8") as fh:
                _ev2 = json.load(fh)
            _bl0_2 = (_ev2.get("baselines") or [{}])[0] if _ev2.get("baselines") else _ev2.get("baseline", {})
            cmd_list = _bl0_2.get("command", [])
            if cmd_list:
                python_interp = cmd_list[0]

        return cmd_promote_driver(
            driver_paths=driver_paths,
            evidence_path=evidence_path,
            python=python_interp,
            pkg_abs=pkg_abs,
            timeout=args.driver_timeout,
        )

    # ── --refusal-report fast-path ────────────────────────────────────────
    if args.refusal_report:
        evidence_path_str = args.evidence_out or str(Path(__file__).parent / "evidence.json")
        evidence_path = Path(evidence_path_str)
        if not evidence_path.is_file():
            print(
                f"[refusal-report] ERROR: evidence file not found: {evidence_path}\n"
                f"  Pass --evidence <path> to specify the evidence.json to read.",
                file=sys.stderr,
            )
            return 1

        # Resolve pkg_abs.
        if args.pkg_for_driver:
            pkg_abs_rr = str(Path(args.pkg_for_driver).resolve())
        else:
            with open(evidence_path, encoding="utf-8") as fh:
                _ev_rr = json.load(fh)
            _bl0_rr = (_ev_rr.get("baselines") or [{}])[0] if _ev_rr.get("baselines") else _ev_rr.get("baseline", {})
            pkg_abs_rr = _bl0_rr.get("package", "")
            if not pkg_abs_rr:
                print(
                    "[refusal-report] ERROR: cannot determine package path. "
                    "Pass --pkg <path> or ensure evidence.json has baselines[0].package set.",
                    file=sys.stderr,
                )
                return 1

        # Resolve python interpreter.
        python_interp_rr = args.python
        if python_interp_rr == sys.executable:
            with open(evidence_path, encoding="utf-8") as fh:
                _ev_rr2 = json.load(fh)
            _bl0_rr2 = (_ev_rr2.get("baselines") or [{}])[0] if _ev_rr2.get("baselines") else _ev_rr2.get("baseline", {})
            cmd_list_rr = _bl0_rr2.get("command", [])
            if cmd_list_rr:
                python_interp_rr = cmd_list_rr[0]

        return cmd_refusal_report(
            drivers_dir=Path(args.drivers_dir),
            evidence_path=evidence_path,
            python=python_interp_rr,
            pkg_abs=pkg_abs_rr,
            timeout=args.driver_timeout,
        )

    # ── --report fast-path: read existing evidence.json, write HTML, done ─
    # Only takes this path when --package is not given (i.e. no coverage run
    # is requested). If --package is also given the report is generated at the
    # end of the full run, after --evidence has been written.
    if args.report_out and not args.package:
        evidence_src = args.evidence_out or str(Path(__file__).parent / "evidence.json")
        if not Path(evidence_src).is_file():
            print(
                f"[first_light] ERROR: evidence file not found: {evidence_src}\n"
                f"  Hint: run first_light.py with --evidence to produce it first,\n"
                f"        or pass --evidence <path> to point at the correct location.",
                file=sys.stderr,
            )
            return 1
        write_html_report(evidence_src, args.report_out)
        return 0

    # ── Full coverage-run path ────────────────────────────────────────────
    if not args.package:
        print("[first_light] ERROR: --package is required when not using --report", file=sys.stderr)
        return 1
    runner_list = args.runners or []
    if not runner_list:
        print("[first_light] ERROR: --runner is required when not using --report", file=sys.stderr)
        return 1

    pkg_path = Path(args.package).resolve()
    if not pkg_path.is_dir():
        print(f"[first_light] ERROR: package path does not exist: {pkg_path}", file=sys.stderr)
        return 1

    exclude_dirs = set(args.exclude or [])

    # Assign ids to each runner.  Explicit --runner-id takes priority.
    runner_ids = list(args.runner_ids or [])
    while len(runner_ids) < len(runner_list):
        runner_ids.append(f"baseline_{len(runner_ids)}")

    python_abs = str(Path(args.python).resolve())
    fl_abs     = str(Path(__file__).resolve())

    # ── collect one baseline per runner ──────────────────────────────────
    collected_baselines: list[BaselineInfo] = []
    for runner_script, bl_id in zip(runner_list, runner_ids):
        runner_path = Path(runner_script).resolve()
        if not runner_path.is_file():
            print(f"[first_light] ERROR: runner script does not exist: {runner_path}", file=sys.stderr)
            return 1

        # Build the command that coverage.py will actually execute for THIS runner.
        # Each baseline's command differs in the runner path, so the two cards
        # in the report will show genuinely different strings.
        runner_cmd = [
            python_abs, "-m", "coverage", "run",
            f"--source={str(pkg_path)}",
            str(runner_path),
        ]

        print(f"[first_light] [{bl_id}] collecting coverage …", file=sys.stderr)
        executed, exit_code, runner_stdout = collect_coverage(
            package_path=str(pkg_path),
            runner_script=str(runner_path),
            python=args.python,
            rcfile=args.rcfile,
        )
        n_files_covered = len(executed)
        print(
            f"[first_light] [{bl_id}] coverage collected for {n_files_covered} source file(s) "
            f"(runner exit code: {exit_code})",
            file=sys.stderr,
        )
        if exit_code not in (0, 1):
            print(
                f"[first_light] WARNING: [{bl_id}] runner exited {exit_code} — "
                f"coverage data may be incomplete.",
                file=sys.stderr,
            )
        elif exit_code == 1:
            print(
                f"[first_light] NOTE: [{bl_id}] runner exited 1 — "
                f"baseline is partial (some tests failed or runner reported warnings). "
                f"Coverage from passing tests is still recorded.",
                file=sys.stderr,
            )
        # Parse pytest counts from stdout if available.
        pytest_collected, pytest_passed, pytest_failed = _parse_pytest_counts(runner_stdout)
        collected_baselines.append(BaselineInfo(
            baseline_id=bl_id,
            runner_script=str(runner_path),
            cmd=runner_cmd,
            runner_cmd=runner_cmd,
            exit_code=exit_code,
            executed=executed,
            pytest_collected=pytest_collected,
            pytest_passed=pytest_passed,
            pytest_failed=pytest_failed,
        ))

    # ── enumerate all functions ───────────────────────────────────────────
    print(f"[first_light] parsing source files …", file=sys.stderr)
    all_py_files = collect_python_files(pkg_path, set())   # no exclusions
    all_funcs: list[FuncInfo] = []
    for py_file in all_py_files:
        all_funcs.extend(iter_functions(py_file, pkg_path))

    # Merge coverage: a function is observed if ANY baseline observed it.
    merged_executed: dict[str, set[int]] = {}
    for bl in collected_baselines:
        for fpath, lines in bl.executed.items():
            if fpath in merged_executed:
                merged_executed[fpath] |= lines
            else:
                merged_executed[fpath] = set(lines)

    all_obs, all_never = classify_functions(all_funcs, merged_executed)
    all_observed_ids = {id(fn) for fn in all_obs}

    # ── product-code enumeration ─────────────────────────────────────────
    prod_py_files = collect_python_files(pkg_path, exclude_dirs)
    prod_funcs: list[FuncInfo] = []
    for py_file in prod_py_files:
        prod_funcs.extend(iter_functions(py_file, pkg_path))

    prod_obs, prod_never = classify_functions(prod_funcs, merged_executed)
    prod_observed_ids = {id(fn) for fn in prod_obs}

    # ── breakdowns ───────────────────────────────────────────────────────
    breakdown = build_breakdown(all_funcs, all_observed_ids, pkg_path)
    prod_breakdown = build_breakdown(prod_funcs, prod_observed_ids, pkg_path)

    # ── render ───────────────────────────────────────────────────────────
    report = render_report(
        all_funcs, all_obs, all_never,
        prod_funcs, prod_obs, prod_never,
        breakdown, prod_breakdown,
        exclude_dirs,
        baselines=collected_baselines,
    )
    print(report)

    # ── evidence.json output ──────────────────────────────────────────────
    if args.evidence_out:
        write_evidence(
            out_path=args.evidence_out,
            pkg_path=pkg_path,
            exclude_dirs=exclude_dirs,
            all_funcs=all_funcs,
            baselines=collected_baselines,
        )
        print(f"[first_light] evidence written to {args.evidence_out}", file=sys.stderr)

    # ── optional HTML report ──────────────────────────────────────────────
    if args.report_out:
        evidence_src = args.evidence_out or str(Path(__file__).parent / "evidence.json")
        if Path(evidence_src).is_file():
            write_html_report(evidence_src, args.report_out)
        else:
            print(
                f"[first_light] WARNING: --report requested but evidence file not found at {evidence_src}. "
                f"Pass --evidence <path> to generate evidence.json first.",
                file=sys.stderr,
            )

    # ── optional JSON output ─────────────────────────────────────────────
    if args.json_out:
        def _make_entry(fn: FuncInfo, obs: bool) -> dict:
            return {
                "qualified_name": fn.qualified_name,
                "file": fn.file,
                "def_line": fn.def_line,
                "body_start": fn.body_start,
                "body_end": fn.body_end,
                "is_async": fn.is_async,
                "observed": obs,
            }

        json_data = {
            "summary": {
                "whole_package": {
                    "total": len(all_funcs),
                    "observed": len(all_obs),
                    "never_observed": len(all_never),
                },
                "product_code": {
                    "excluded_dirs": sorted(exclude_dirs),
                    "total": len(prod_funcs),
                    "observed": len(prod_obs),
                    "never_observed": len(prod_never),
                },
            },
            "breakdown_by_directory": breakdown,
            "functions": (
                [_make_entry(fn, True) for fn in all_obs] +
                [_make_entry(fn, False) for fn in all_never]
            ),
        }
        with open(args.json_out, "w") as jf:
            json.dump(json_data, jf, indent=2)
        print(f"[first_light] JSON results written to {args.json_out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
