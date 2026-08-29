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
    units: dict[str, dict] = {}
    for fn in all_funcs:
        key = f"{fn.file}::{fn.qualified_name}"
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
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="First Light — observe which Python functions execute.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--package", required=True,
        help="Path to the Python package directory to analyse (e.g. target/visidata/visidata)",
    )
    parser.add_argument(
        "--runner", required=True,
        help="Python script that runs the target application",
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

    args = parser.parse_args(argv)

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
