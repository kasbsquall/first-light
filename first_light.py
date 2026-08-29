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
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

FIRST_LIGHT_VERSION = "0.3.0"

# Valid provenance values — the only four that may appear in evidence.json.
# never_observed      : nothing we ran ever entered this function body.
# observed_in_situ    : the system executed it on its own, under normal operation.
# observed_under_driver: it only ran because we built something to reach it.
# superseded          : a driver existed for this unit, but a later baseline also
#                       reached it in situ, making the driver redundant.  The driver
#                       is kept; this provenance records that it is no longer the only
#                       evidence.
PROVENANCE_NEVER           = "never_observed"
PROVENANCE_IN_SITU         = "observed_in_situ"
PROVENANCE_UNDER_DRIVER    = "observed_under_driver"
PROVENANCE_SUPERSEDED      = "superseded"


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
    ) -> None:
        self.id = baseline_id
        self.runner_script = runner_script
        self.cmd = cmd
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
      command           : full argv list used to produce the data.
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
      provenance        : one of PROVENANCE_* constants.
                          A unit is observed_in_situ when ANY baseline observed
                          it (unless promoted to observed_under_driver).
                          A unit is superseded when it was previously
                          observed_under_driver but a baseline now also
                          reaches it in situ.
      observed_in_baseline : list of baseline ids that observed this unit.
                          An empty list means never_observed.
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
            "command": bl.cmd,
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
  --never:       #2C2825;
  --never-border:#3D3730;
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
.scope-panel {
  display: flex;
  gap: 32px;
  margin-top: 24px;
  margin-left: 4px;
  flex-wrap: wrap;
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

/* Map rows */
.map-row {
  display: flex;
  align-items: flex-start;
  margin-bottom: 8px;
  gap: 12px;
}

.map-row__label {
  width: 200px;
  flex-shrink: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  padding-top: 1px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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
}

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
"""


def _strip_def_line_suffix(qname: str) -> str:
    """Strip the '#<lineno>' suffix appended to unit keys before showing to humans."""
    if "#" in qname:
        return qname.rsplit("#", 1)[0]
    return qname


def _driver_units_from_evidence(
    units: dict,
    drivers_dir: Path | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Return (confirmed, superseded, attempted_names).

    confirmed      : list of dicts with keys name, call_site, coverage_confirmed_lines
                     for every unit whose provenance is observed_under_driver.
    superseded     : list of dicts with keys name, superseded_by for every unit
                     whose provenance is superseded (driver existed; a baseline now
                     also covers it in situ, making the driver redundant).
    attempted_names: list of qualified names whose driver file exists on disk but
                     whose unit is NOT observed_under_driver or superseded in evidence
                     — i.e. drivers that were attempted but not confirmed.
    """
    confirmed: list[dict] = []
    superseded: list[dict] = []
    confirmed_or_superseded_qnames: set[str] = set()

    for key, u in units.items():
        prov = u.get("provenance")
        if prov not in (PROVENANCE_UNDER_DRIVER, PROVENANCE_SUPERSEDED):
            continue
        qname_part = key.split("::", 1)[1] if "::" in key else key
        qname_part = _strip_def_line_suffix(qname_part)
        if prov == PROVENANCE_UNDER_DRIVER:
            confirmed.append({
                "name": qname_part,
                "call_site": u.get("call_site", ""),
                "coverage_confirmed_lines": u.get("coverage_confirmed_lines", []),
            })
        else:  # PROVENANCE_SUPERSEDED
            superseded.append({
                "name": qname_part,
                "superseded_by": u.get("superseded_by", ""),
                "call_site": u.get("call_site", ""),
                "coverage_confirmed_lines": u.get("coverage_confirmed_lines", []),
            })
        confirmed_or_superseded_qnames.add(qname_part)

    # Attempted: driver file exists but unit is not observed_under_driver or superseded.
    # Build a map of qname_suffix -> unit provenance from evidence so we can
    # cross-reference against files on disk.
    attempted_names: list[str] = []
    if drivers_dir is not None and drivers_dir.is_dir():
        evidence_qnames: dict[str, str] = {}  # qname_suffix -> provenance
        for key, u in units.items():
            if "::" not in key:
                continue
            qname_part = key.split("::", 1)[1]
            qname_part = _strip_def_line_suffix(qname_part)
            evidence_qnames[qname_part] = u.get("provenance", PROVENANCE_NEVER)

        for driver_file in sorted(drivers_dir.glob("*.py")):
            stem = driver_file.stem
            prov = evidence_qnames.get(stem)
            # "Attempted" = driver file exists but not promoted or superseded.
            # (None means the driver has no matching unit in evidence at all —
            # also worth reporting.)
            if prov not in (PROVENANCE_UNDER_DRIVER, PROVENANCE_SUPERSEDED):
                attempted_names.append(stem)

    # Sort lists for stable output
    confirmed.sort(key=lambda d: d["name"])
    superseded.sort(key=lambda d: d["name"])
    return confirmed, superseded, attempted_names


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
    insitu_count = sum(1 for u in units.values() if u["provenance"] == PROVENANCE_IN_SITU)
    driver_count = sum(1 for u in units.values() if u["provenance"] == PROVENANCE_UNDER_DRIVER)
    superseded_count = sum(1 for u in units.values() if u["provenance"] == PROVENANCE_SUPERSEDED)

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
    prod_insitu_count = sum(1 for u in prod_units.values() if u["provenance"] == PROVENANCE_IN_SITU)
    prod_driver_count = sum(1 for u in prod_units.values() if u["provenance"] == PROVENANCE_UNDER_DRIVER)
    prod_superseded_count = sum(1 for u in prod_units.values() if u["provenance"] == PROVENANCE_SUPERSEDED)

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
    seen_by_prior: set[str] = set()
    for bl in raw_baselines:
        bl_id = bl["id"]
        additive = 0
        for u in prod_units.values():
            obs = u.get("observed_in_baseline", [])
            if bl_id in obs and not any(p in obs for p in seen_by_prior):
                additive += 1
        prod_bl_additive[bl_id] = additive
        seen_by_prior.add(bl_id)

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
        })

    # Sort modules: observed-first, then alphabetically
    def _module_sort_key(item):
        label, funcs = item
        obs = sum(1 for f in funcs if f["provenance"] != PROVENANCE_NEVER)
        return (-obs, label)

    sorted_modules = sorted(module_map.items(), key=_module_sort_key)

    # ── Driver info ───────────────────────────────────────────────────────────
    # confirmed : units with provenance=observed_under_driver (coverage verified).
    # superseded: driver existed but a baseline now also covers the unit in situ.
    # attempted : driver files that exist on disk but are not yet confirmed.
    _drivers_dir = Path(__file__).parent / "drivers"
    confirmed_drivers, superseded_drivers, attempted_drivers = _driver_units_from_evidence(units, _drivers_dir)

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
        cmd_tokens = [_rel(str(c)) if (os.sep in str(c) or "/" in str(c)) else str(c) for c in command_raw]
        cmd_str = " ".join(cmd_tokens) if cmd_tokens else ""
        exit_code = bl.get("exit_code", 0)
        exit_color = "var(--amber)" if exit_code != 0 else "var(--text)"
        partial_note = (
            f' <span style="color:{exit_color};font-weight:600;">[exit {exit_code} — partial run]</span>'
            if exit_code != 0 else ""
        )
        bl_obs = prod_bl_counts.get(bl_id, 0)
        bl_add = prod_bl_additive.get(bl_id, 0)
        add_note = f" (+{bl_add} unique)" if bl_add > 0 else (" (no unique additions)" if raw_baselines.index(bl) > 0 else "")
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
  <div class="label">Baseline &mdash; {e(bl_id)}{partial_note}</div>
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
            if prov == PROVENANCE_IN_SITU:
                cls = "cell cell--insitu"
            elif prov == PROVENANCE_UNDER_DRIVER:
                cls = "cell cell--driver"
            else:
                cls = "cell cell--never"
            title = e(fn["qname"])
            cells.append(f'<span class="{cls}" title="{title}"></span>')
        cells_html = "\n".join(cells)
        label_html = e(mod_label)
        map_rows_html.append(
            f'<div class="map-row">'
            f'<span class="map-row__label" title="{label_html}">{label_html}</span>'
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

    # Superseded driver cards
    superseded_cards_html = []
    for d in superseded_drivers:
        sup_by = d.get("superseded_by", "")
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
        superseded_cards_html.append(
            f'<div class="driver-card" style="border-color:#3a3a20;opacity:0.85;">'
            f'<div class="driver-card__name" style="color:#b8a030;">{e(d["name"])}</div>'
            f'<div class="driver-card__call">{call_html}</div>'
            f'<div class="driver-card__lines" style="margin-top:6px;">'
            f'superseded by baseline: <strong style="color:var(--text);">{e(sup_by)}</strong> — '
            f'driver is no longer the only evidence for this unit'
            f'</div>'
            f'</div>'
        )
    superseded_html = "\n".join(superseded_cards_html)

    # Attempted-but-not-confirmed driver cards
    attempted_cards_html = []
    for name in attempted_drivers:
        attempted_cards_html.append(
            f'<div class="driver-card" style="border-color:#5a2020;">'
            f'<div class="driver-card__name" style="color:#cc4444;">{e(name)}</div>'
            f'<div class="driver-card__call" style="color:var(--muted);">driver file exists — not yet confirmed under coverage</div>'
            f'</div>'
        )
    attempted_html = "\n".join(attempted_cards_html)

    # Legend: only show driver swatch if driver evidence exists
    driver_legend_item = ""
    if driver_count > 0 or superseded_count > 0:
        driver_legend_item = (
            '<div class="legend__item">'
            '<span class="swatch swatch--driver"></span>'
            'observed under driver'
            '</div>'
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>First Light &mdash; Evidence Report</title>
<style>
{_HTML_CSS}
</style>
</head>
<body>

<!-- ═══════════════════════════════════════════════════════
     SECTION 1 — HEADLINE NUMBER
     ═══════════════════════════════════════════════════════ -->
<header class="headline">
  <span class="label">First Light &mdash; Function Observation Report</span>
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
      <span class="ctx-item__word">in-situ (product)</span>
    </div>
    <div class="ctx-item">
      <span class="ctx-item__num">{e(str(prod_driver_count))}</span>
      <span class="ctx-item__word">under driver (product)</span>
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
        <span class="scope-block__key">observed</span>
        <span class="scope-block__val">{e(str(prod_observed))}</span>
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
        <span class="scope-block__key">observed</span>
        <span class="scope-block__val">{e(str(observed_count))}</span>
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
        <span class="scope-block__key">unique addition</span>
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
<section>
  <div class="label" style="margin-bottom:12px;">Baselines — exactly what was executed</div>
  <div style="font-size:11px;color:var(--muted);margin-bottom:16px;">
    generated {e(generated_at)} &nbsp;&middot;&nbsp; First Light v{e(version)}
  </div>
{all_baselines_html}
</section>

<!-- ═══════════════════════════════════════════════════════
     SECTION 3 — EVIDENCE MAP
     ═══════════════════════════════════════════════════════ -->
<section class="map-section">
  <div class="section-heading">Evidence map — every module, every function</div>
  <div class="legend">
    <div class="legend__item">
      <span class="swatch swatch--insitu"></span>
      observed in situ
    </div>
    {driver_legend_item}
    <div class="legend__item">
      <span class="swatch swatch--never"></span>
      never observed
    </div>
  </div>
  <div class="map">
{map_html}
  </div>
</section>

<hr class="sep">

<!-- ═══════════════════════════════════════════════════════
     SECTION 4 — DRIVER RESULTS
     ═══════════════════════════════════════════════════════ -->
<section class="drivers-section">
  <div class="section-heading">Driver results — {e(str(len(confirmed_drivers)))} confirmed, {e(str(len(superseded_drivers)))} superseded by baseline, {e(str(len(attempted_drivers)))} not confirmed</div>
{drivers_html}
{f'<div style="margin-top:28px"><div class="section-heading" style="color:#b8a030;">Superseded — baseline now reaches these in situ ({e(str(len(superseded_drivers)))})</div><p style="font-size:12px;color:var(--muted);margin-bottom:12px;">The second baseline made these drivers redundant. The driver is not deleted; this group records that it is no longer the only evidence.</p>' + superseded_html + '</div>' if superseded_drivers else ''}
{f'<div style="margin-top:20px"><div class="section-heading" style="color:#cc4444;">Attempted — not confirmed ({e(str(len(attempted_drivers)))})</div>' + attempted_html + '</div>' if attempted_drivers else ''}
</section>

<footer class="footer">
  Made with IBM Bob &mdash; First Light v{e(version)} &mdash; {e(generated_at)}
</footer>

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


def _resolve_unit_for_driver(
    driver_path: Path,
    evidence_path: Path,
) -> tuple[str | None, dict | None]:
    """Return (unit_key, unit_dict) for the driver, or (None, None) on failure."""
    # Driver filename encodes the qualified name: visidata.utils.moveListItem.py
    # → qualified name suffix "visidata.utils.moveListItem"
    qname_suffix = driver_path.stem  # strip .py

    with open(evidence_path, encoding="utf-8") as fh:
        doc = json.load(fh)

    units: dict = doc.get("units", {})
    for key, unit in units.items():
        # Key format: <file>::<qualified_name>#<def_line>
        # The qualified_name portion (between :: and #) must end with the suffix.
        if "::" not in key:
            continue
        qname_part = key.split("::", 1)[1]
        if "#" in qname_part:
            qname_part = qname_part.rsplit("#", 1)[0]
        if qname_part == qname_suffix:
            return key, unit

    return None, None


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
    promoted = 0
    failed   = 0

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
            print(
                f"[promote-driver] FAIL  {driver_path.name} — "
                f"no unit found for qualified name '{driver_path.stem}' in {evidence_path}",
                file=sys.stderr,
            )
            failed += 1
            continue

        # ── handle already-promoted or superseded ─────────────────────────
        current_prov = unit.get("provenance")
        if current_prov == PROVENANCE_IN_SITU:
            # The unit is now reached by a baseline: the driver is redundant.
            # Record it as superseded so it's visible in the report.
            # Determine which baselines observed it.
            bl_ids_that_cover = unit.get("observed_in_baseline", [])
            superseded_by = bl_ids_that_cover[0] if bl_ids_that_cover else "unknown"
            # Extract call site from driver even though we won't do a full
            # coverage run — we still want to store the driver metadata.
            call_site_text, _ = _driver_call_site(driver_path)
            doc["units"][unit_key]["provenance"]   = PROVENANCE_SUPERSEDED
            doc["units"][unit_key]["driver"]        = str(driver_path.resolve())
            doc["units"][unit_key]["call_site"]     = call_site_text
            doc["units"][unit_key]["superseded_by"] = superseded_by
            print(
                f"[promote-driver] SUPERSEDED  {driver_path.name} — "
                f"unit now reached by baseline '{superseded_by}'; driver is redundant",
                file=sys.stderr,
            )
            promoted += 1
            continue
        if current_prov == PROVENANCE_UNDER_DRIVER:
            print(
                f"[promote-driver] SKIP  {driver_path.name} — already observed_under_driver",
                file=sys.stderr,
            )
            continue
        if current_prov == PROVENANCE_SUPERSEDED:
            print(
                f"[promote-driver] SKIP  {driver_path.name} — already superseded",
                file=sys.stderr,
            )
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
            reason = {
                "not_reached": f"driver ran but zero body lines ({body_start}-{body_end}) hit in {src_abs}",
                "crash":       "driver process exited with non-zero return code",
                "cov_fail":    "coverage JSON export failed",
            }.get(status, status)
            print(
                f"[promote-driver] FAIL  {driver_path.name} — {reason}",
                file=sys.stderr,
            )
            failed += 1
            continue

        # ── extract call site from driver comment ─────────────────────────
        call_site, is_indirect = _driver_call_site(driver_path)

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
        if call_site:
            if is_indirect:
                # ── indirect call site: two segments separated by " ; " ──
                segments = [s.strip() for s in call_site.split(";")]
                if len(segments) != 2:
                    print(
                        f"[promote-driver] FAIL  {driver_path.name} -- "
                        f"indirect call site must have exactly two segments separated by ';': "
                        f"'dispatch_file:N -- text ; binding_file:M -- binding_name'. "
                        f"Correct the '# call site (indirect):' comment.",
                        file=sys.stderr,
                    )
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
                for seg_label, seg_text, check_name in [
                    ("dispatch", dispatch_seg, False),
                    ("binding",  binding_seg,  True),
                ]:
                    seg_match = _re.match(r"^(.+):(\d+)", seg_text)
                    if not seg_match:
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- "
                            f"indirect call site {seg_label} segment '{seg_text}' "
                            f"does not match 'file:N -- ...' format.",
                            file=sys.stderr,
                        )
                        cs_ok = False
                        break
                    seg_file_raw = seg_match.group(1).strip()
                    seg_line = int(seg_match.group(2))
                    seg_file_path = Path(seg_file_raw)
                    if not seg_file_path.is_absolute():
                        seg_file_path = Path(".").resolve() / seg_file_raw
                    if not seg_file_path.is_file():
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- "
                            f"indirect call site {seg_label} file '{seg_file_raw}' does not exist.",
                            file=sys.stderr,
                        )
                        cs_ok = False
                        break
                    try:
                        seg_text_lines = seg_file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        if seg_line < 1 or seg_line > len(seg_text_lines):
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- "
                                f"indirect call site {seg_label} line {seg_line} is out of range "
                                f"for '{seg_file_raw}' ({len(seg_text_lines)} lines).",
                                file=sys.stderr,
                            )
                            cs_ok = False
                            break
                        seg_line_text = seg_text_lines[seg_line - 1]
                        if check_name and func_simple_name not in seg_line_text:
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- "
                                f"function name '{func_simple_name}' not found on "
                                f"{seg_label} line {seg_line} of '{seg_file_raw}': "
                                f"{seg_line_text.strip()!r}. "
                                f"Correct the '# call site (indirect):' comment.",
                                file=sys.stderr,
                            )
                            cs_ok = False
                            break
                        # Rule 3: neither line may fall inside the function's own body.
                        if Path(seg_file_raw).resolve() == Path(src_abs).resolve() and body_start <= seg_line <= body_end:
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- "
                                f"indirect call site {seg_label} line {seg_line} falls inside "
                                f"the target function's own body range ({body_start}-{body_end}).",
                                file=sys.stderr,
                            )
                            cs_ok = False
                            break
                    except OSError as _cs_err:
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- "
                            f"could not read indirect call site {seg_label} file "
                            f"'{seg_file_raw}': {_cs_err}",
                            file=sys.stderr,
                        )
                        cs_ok = False
                        break

                if not cs_ok:
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
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- "
                            f"call site file '{cs_file_raw}' does not exist; "
                            f"correct the '# call site:' comment in the driver file.",
                            file=sys.stderr,
                        )
                        failed += 1
                        continue

                    # Rule 2: the target function's simple name must appear on that line.
                    func_simple_name = unit_key.split("::", 1)[1].rsplit(".", 1)[-1] if "::" in unit_key else unit_key
                    if "#" in func_simple_name:
                        func_simple_name = func_simple_name.rsplit("#", 1)[0]
                    try:
                        cs_lines = cs_file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        if cs_line < 1 or cs_line > len(cs_lines):
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- "
                                f"call site line {cs_line} is out of range for "
                                f"'{cs_file_raw}' ({len(cs_lines)} lines); "
                                f"correct the '# call site:' comment.",
                                file=sys.stderr,
                            )
                            failed += 1
                            continue
                        cs_text = cs_lines[cs_line - 1]
                        if func_simple_name not in cs_text:
                            print(
                                f"[promote-driver] FAIL  {driver_path.name} -- "
                                f"function name '{func_simple_name}' not found on "
                                f"line {cs_line} of '{cs_file_raw}'; "
                                f"correct the '# call site:' comment in the driver file.",
                                file=sys.stderr,
                            )
                            failed += 1
                            continue
                    except OSError as _cs_err:
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- "
                            f"could not read call site file '{cs_file_raw}': {_cs_err}",
                            file=sys.stderr,
                        )
                        failed += 1
                        continue

                    # Rule 3: call site must not fall inside the target's own body.
                    if Path(cs_file_raw).resolve() == Path(src_abs).resolve() and body_start <= cs_line <= body_end:
                        print(
                            f"[promote-driver] FAIL  {driver_path.name} -- "
                            f"call site line {cs_line} falls inside the target function's "
                            f"own body range ({body_start}-{body_end}); a function cannot "
                            f"be its own caller.  Correct the '# call site:' comment "
                            f"in the driver file.",
                            file=sys.stderr,
                        )
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
    if promoted > 0:
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
    total = promoted + failed
    print(
        f"\n[promote-driver] {promoted}/{total} promoted, {failed} failed",
        file=sys.stderr,
    )
    return 0 if failed == 0 else 1


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

    # Base invocation command (shared prefix; runner-specific part appended per baseline).
    base_cmd = [str(Path(args.python).resolve()), str(Path(__file__).resolve())] + (argv or sys.argv[1:])

    # ── collect one baseline per runner ──────────────────────────────────
    collected_baselines: list[BaselineInfo] = []
    for runner_script, bl_id in zip(runner_list, runner_ids):
        runner_path = Path(runner_script).resolve()
        if not runner_path.is_file():
            print(f"[first_light] ERROR: runner script does not exist: {runner_path}", file=sys.stderr)
            return 1

        print(f"[first_light] [{bl_id}] collecting coverage …", file=sys.stderr)
        executed, exit_code = collect_coverage(
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
        collected_baselines.append(BaselineInfo(
            baseline_id=bl_id,
            runner_script=str(runner_path),
            cmd=base_cmd,
            exit_code=exit_code,
            executed=executed,
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
