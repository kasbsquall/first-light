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

FIRST_LIGHT_VERSION = "0.1.0"

# Valid provenance values — the only three that may appear in evidence.json.
# never_observed      : nothing we ran ever entered this function body.
# observed_in_situ    : the system executed it on its own, under normal operation.
# observed_under_driver: it only ran because we built something to reach it.
#                        Not yet produced, but the schema reserves it from day one.
PROVENANCE_NEVER           = "never_observed"
PROVENANCE_IN_SITU         = "observed_in_situ"
PROVENANCE_UNDER_DRIVER    = "observed_under_driver"


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
) -> dict[str, set[int]]:
    """Run *runner_script* under coverage.py and return {abs_path: {executed_lines}}.

    A thin wrapper script is written to a temp file before invoking coverage.
    The wrapper patches os._exit → SystemExit so that targets which call
    os._exit() (like visidata's vd_cli) cannot prevent coverage from writing
    its data file.  A warning is emitted to stderr when the patch fires.
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
        if result.returncode not in (0, 1):
            # rc 1 is acceptable (e.g. visidata batch mode exits 1 on warnings)
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
            return {}

        with open(json_path) as fh:
            data = json.load(fh)

        executed: dict[str, set[int]] = {}
        for file_path, file_data in data.get("files", {}).items():
            abs_path = str(Path(file_path).resolve())
            executed_lines = set(file_data.get("executed_lines", []))
            executed[abs_path] = executed_lines

    return executed


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


def write_evidence(
    out_path: str,
    pkg_path: Path,
    runner_script: str,
    python: str,
    cmd: list[str],
    exclude_dirs: set[str],
    all_funcs: list[FuncInfo],
    all_obs_ids: set[int],
) -> None:
    """Write the evidence.json artifact to *out_path*.

    Schema fields
    -------------
    generated_at        : ISO-8601 UTC timestamp.
    first_light_version : semver string from FIRST_LIGHT_VERSION.
    baseline            : what was executed to produce this observation.
      runner            : absolute path to the runner script.
      package           : absolute path to the analysed package.
      command           : full argv list used to produce the data.
      excluded_dirs     : directory names excluded from product-code figure.
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
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # ── integrity: hash every source file that was analysed ──────────────
    # We collect all unique file paths from all_funcs (whole-package scan).
    seen_files: set[str] = {fn.file for fn in all_funcs}
    integrity: dict[str, str] = {}
    for fpath in sorted(seen_files):
        try:
            integrity[fpath] = _sha256(fpath)
        except OSError:
            integrity[fpath] = ""   # file unreadable; hook will flag as stale

    # ── units: one entry per function ────────────────────────────────────
    # Key includes def_line so that a @property getter and its setter, which
    # share both file and qualified_name, never collide and silently drop one.
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
        provenance = PROVENANCE_IN_SITU if id(fn) in all_obs_ids else PROVENANCE_NEVER
        units[key] = {
            "file": fn.file,
            "def_line": fn.def_line,
            "body_start": fn.body_start,
            "body_end": fn.body_end,
            "provenance": provenance,
        }

    doc = {
        "generated_at": now,
        "first_light_version": FIRST_LIGHT_VERSION,
        "baseline": {
            "runner": str(Path(runner_script).resolve()),
            "package": str(pkg_path.resolve()),
            "command": cmd,
            "excluded_dirs": sorted(exclude_dirs),
        },
        "integrity": integrity,
        "units": units,
    }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)


# ---------------------------------------------------------------------------
# HTML report generator  (reads evidence.json, writes self-contained HTML)
# ---------------------------------------------------------------------------

def _driver_call_site(driver_path: Path) -> str:
    """Extract the first 'call site:' comment from a driver file, or ''."""
    try:
        text = driver_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped.lower().startswith("call site:"):
                return stripped[len("call site:"):].strip()
        # Fallback: look for 'Real call site' / 'call site' in docstring lines
        for line in text.splitlines():
            lower = line.lower()
            if "call site" in lower and "--" in line:
                after = line[line.index("--"):].strip(" -")
                if after:
                    return after
    except OSError:
        pass
    return ""


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


def _driver_units_from_evidence(units: dict) -> tuple[list[dict], list[str]]:
    """Return (confirmed, attempted_names) derived exclusively from evidence.

    confirmed      : list of dicts with keys name, call_site, coverage_confirmed_lines
                     for every unit whose provenance is observed_under_driver.
    attempted_names: list of driver file stem names that exist on disk but whose
                     unit is NOT observed_under_driver in evidence (they were
                     attempted but not confirmed).
    """
    confirmed: list[dict] = []
    confirmed_qnames: set[str] = set()

    for key, u in units.items():
        if u.get("provenance") != PROVENANCE_UNDER_DRIVER:
            continue
        # Derive a display name: qualified_name portion of the unit key
        qname_part = key.split("::", 1)[1] if "::" in key else key
        if "#" in qname_part:
            qname_part = qname_part.rsplit("#", 1)[0]
        confirmed.append({
            "name": qname_part,
            "call_site": u.get("call_site", ""),
            "coverage_confirmed_lines": u.get("coverage_confirmed_lines", []),
        })
        confirmed_qnames.add(qname_part)

    # Sort confirmed list for stable output
    confirmed.sort(key=lambda d: d["name"])
    return confirmed, []


def write_html_report(evidence_path: str, out_path: str) -> None:
    """Read *evidence_path* (evidence.json) and write a self-contained HTML report to *out_path*."""
    import html as _html

    with open(evidence_path, encoding="utf-8") as fh:
        ev = json.load(fh)

    baseline = ev.get("baseline", {})
    units = ev.get("units", {})
    generated_at = ev.get("generated_at", "")
    version = ev.get("first_light_version", "")

    # ── Compute summary numbers ──────────────────────────────────────────────
    # Whole-package (all units, including tests/apps/experimental/vendor)
    total = len(units)
    never_count = sum(1 for u in units.values() if u["provenance"] == PROVENANCE_NEVER)
    observed_count = total - never_count
    insitu_count = sum(1 for u in units.values() if u["provenance"] == PROVENANCE_IN_SITU)
    driver_count = sum(1 for u in units.values() if u["provenance"] == PROVENANCE_UNDER_DRIVER)

    # Product-code only (exclude the same dirs the baseline recorded)
    excluded_dirs: list[str] = baseline.get("excluded_dirs", [])
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

    # ── Build per-module map data ────────────────────────────────────────────
    # Group functions by their source file (relative path stripped to module name)
    pkg_path_str = baseline.get("package", "")
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
        module_map[mod_label].append({
            "qname": key.split("::")[-1] if "::" in key else key,
            "provenance": u["provenance"],
        })

    # Sort modules: observed-first, then alphabetically
    def _module_sort_key(item):
        label, funcs = item
        obs = sum(1 for f in funcs if f["provenance"] != PROVENANCE_NEVER)
        return (-obs, label)

    sorted_modules = sorted(module_map.items(), key=_module_sort_key)

    # ── Driver info — derived exclusively from evidence.json ─────────────────
    # A driver file existing on disk proves nothing about whether the function
    # was reached; only units with provenance=observed_under_driver count.
    confirmed_drivers, _attempted = _driver_units_from_evidence(units)

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

    runner = _rel(baseline.get("runner", ""))
    package = _rel(baseline.get("package", ""))
    command_raw = baseline.get("command", [])
    # Relativise each token in the command that looks like an absolute path.
    command = [_rel(str(c)) if (os.sep in str(c) or "/" in str(c)) else str(c) for c in command_raw]
    excluded = baseline.get("excluded_dirs", [])

    cmd_str = " ".join(command) if command else ""
    excl_str = ", ".join(excluded) if excluded else "none"
    prod_scope_label = ("Product code (excl. " + excl_str + ")") if excl_str and excl_str != "none" else "Product code"

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

    # Legend: only show driver swatch if driver evidence exists
    driver_legend_item = ""
    if driver_count > 0:
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
      <span class="ctx-item__num">{e(str(insitu_count))}</span>
      <span class="ctx-item__word">in-situ</span>
    </div>
    <div class="ctx-item">
      <span class="ctx-item__num">{e(str(driver_count))}</span>
      <span class="ctx-item__word">under driver</span>
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
  </div>
</header>

<hr class="sep">

<!-- ═══════════════════════════════════════════════════════
     SECTION 2 — BASELINE
     ═══════════════════════════════════════════════════════ -->
<section class="baseline">
  <div class="label">Baseline — exactly what was executed</div>
  <div class="baseline__grid">
    <span class="baseline__key">runner</span>
    <span class="baseline__val">{e(runner)}</span>

    <span class="baseline__key">package</span>
    <span class="baseline__val">{e(package)}</span>

    <span class="baseline__key">command</span>
    <span class="baseline__val baseline__val--cmd">{e(cmd_str)}</span>

    <span class="baseline__key">excluded</span>
    <span class="baseline__val">{e(excl_str)}</span>

    <span class="baseline__key">generated</span>
    <span class="baseline__val">{e(generated_at)}</span>

    <span class="baseline__key">version</span>
    <span class="baseline__val">{e(version)}</span>
  </div>
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
  <div class="section-heading">Driver results ({e(str(len(confirmed_drivers)))} confirmed)</div>
{drivers_html}
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

        if run_result.returncode not in (0, 1):
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
      4. On success, write observed_under_driver + confirmed lines + driver path
         + call site into evidence.json.
      5. On failure, leave the unit as never_observed and report the reason.

    Returns 0 if every driver succeeded, 1 if any failed.
    """
    promoted = 0
    failed   = 0

    for driver_path in driver_paths:
        driver_abs = str(driver_path.resolve())
        print(f"[promote-driver] processing {driver_path.name} …", file=sys.stderr)

        # ── resolve unit ──────────────────────────────────────────────────
        unit_key, unit = _resolve_unit_for_driver(driver_path, evidence_path)
        if unit_key is None:
            print(
                f"[promote-driver] FAIL  {driver_path.name} — "
                f"no unit found for qualified name '{driver_path.stem}' in {evidence_path}",
                file=sys.stderr,
            )
            failed += 1
            continue

        # ── skip already promoted ─────────────────────────────────────────
        if unit.get("provenance") == PROVENANCE_IN_SITU:
            print(
                f"[promote-driver] SKIP  {driver_path.name} — already observed_in_situ",
                file=sys.stderr,
            )
            continue
        if unit.get("provenance") == PROVENANCE_UNDER_DRIVER:
            print(
                f"[promote-driver] SKIP  {driver_path.name} — already observed_under_driver",
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
                "crash":       "driver process exited with non-0/1 return code",
                "cov_fail":    "coverage JSON export failed",
            }.get(status, status)
            print(
                f"[promote-driver] FAIL  {driver_path.name} — {reason}",
                file=sys.stderr,
            )
            failed += 1
            continue

        # ── extract call site from driver comment ─────────────────────────
        call_site = _driver_call_site(driver_path)

        # ── validate call site does not fall inside the target's own body ─
        # A function cannot be its own caller.  If the line number extracted
        # from the call site comment falls within [body_start, body_end] the
        # comment is wrong and we reject rather than store false data.
        if call_site:
            import re as _re
            _cs_match = _re.search(r":(\d+)", call_site)
            if _cs_match:
                cs_line = int(_cs_match.group(1))
                if body_start <= cs_line <= body_end:
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

        # ── write back to evidence.json ───────────────────────────────────
        with open(evidence_path, encoding="utf-8") as fh:
            doc = json.load(fh)

        doc["units"][unit_key]["provenance"]               = PROVENANCE_UNDER_DRIVER
        doc["units"][unit_key]["driver"]                   = driver_abs
        doc["units"][unit_key]["call_site"]                = call_site
        doc["units"][unit_key]["coverage_confirmed_lines"] = confirmed_lines

        with open(evidence_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

        print(
            f"[promote-driver] OK    {driver_path.name} — "
            f"lines {confirmed_lines[:4]}{'...' if len(confirmed_lines) > 4 else ''}",
            file=sys.stderr,
        )
        promoted += 1

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
        "--runner", default=None,
        help="Python script that runs the target application. "
             "Required unless --report is given.",
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
            pkg_abs = _ev.get("baseline", {}).get("package", "")
            if not pkg_abs:
                print(
                    "[promote-driver] ERROR: cannot determine package path. "
                    "Pass --pkg <path> or ensure evidence.json has baseline.package set.",
                    file=sys.stderr,
                )
                return 1

        # Resolve python interpreter: --python takes priority, else read from evidence.json.
        python_interp = args.python
        if python_interp == sys.executable:
            # user did not explicitly pass --python; check evidence baseline
            with open(evidence_path, encoding="utf-8") as fh:
                _ev2 = json.load(fh)
            cmd_list = _ev2.get("baseline", {}).get("command", [])
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
    if not args.runner:
        print("[first_light] ERROR: --runner is required when not using --report", file=sys.stderr)
        return 1

    pkg_path = Path(args.package).resolve()
    if not pkg_path.is_dir():
        print(f"[first_light] ERROR: package path does not exist: {pkg_path}", file=sys.stderr)
        return 1

    runner_path = Path(args.runner).resolve()
    if not runner_path.is_file():
        print(f"[first_light] ERROR: runner script does not exist: {runner_path}", file=sys.stderr)
        return 1

    exclude_dirs = set(args.exclude or [])

    # Reconstruct the command that was used to produce this observation.
    # Stored verbatim in evidence.json so the claim "never observed" is
    # traceable to exactly what was run.
    invocation_cmd = [str(Path(args.python).resolve()), str(Path(__file__).resolve())] + (argv or sys.argv[1:])

    print(f"[first_light] collecting coverage …", file=sys.stderr)
    executed = collect_coverage(
        package_path=str(pkg_path),
        runner_script=str(runner_path),
        python=args.python,
        rcfile=args.rcfile,
    )
    n_files_covered = len(executed)
    print(f"[first_light] coverage collected for {n_files_covered} source file(s)", file=sys.stderr)

    # ── whole-package enumeration ────────────────────────────────────────
    print(f"[first_light] parsing source files …", file=sys.stderr)
    all_py_files = collect_python_files(pkg_path, set())   # no exclusions
    all_funcs: list[FuncInfo] = []
    for py_file in all_py_files:
        all_funcs.extend(iter_functions(py_file, pkg_path))

    all_obs, all_never = classify_functions(all_funcs, executed)
    all_observed_ids = {id(fn) for fn in all_obs}

    # ── product-code enumeration ─────────────────────────────────────────
    prod_py_files = collect_python_files(pkg_path, exclude_dirs)
    prod_funcs: list[FuncInfo] = []
    for py_file in prod_py_files:
        prod_funcs.extend(iter_functions(py_file, pkg_path))

    prod_obs, prod_never = classify_functions(prod_funcs, executed)
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
    )
    print(report)

    # ── evidence.json output ──────────────────────────────────────────────
    if args.evidence_out:
        write_evidence(
            out_path=args.evidence_out,
            pkg_path=pkg_path,
            runner_script=str(runner_path),
            python=args.python,
            cmd=invocation_cmd,
            exclude_dirs=exclude_dirs,
            all_funcs=all_funcs,
            all_obs_ids=all_observed_ids,
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
