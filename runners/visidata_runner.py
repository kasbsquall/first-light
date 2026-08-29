#!/usr/bin/env python3
"""
Runner for First Light: exercises visidata in --batch mode.

Runs visidata against sample_data/benchmark.csv, saving the output to a
temporary file, then exits.  Designed to be invoked via `coverage run`.

Strategy: we call main_vd() directly (not vd_cli()) so that coverage.py can
flush its data file before the process terminates.  vd_cli() wraps main_vd()
but ends with os._exit(), which kills the process before any atexit/finalizer
runs -- coverage never writes its .coverage file, so zero lines are recorded.
"""

import os
import sys
import tempfile
from pathlib import Path

# ── locate the sample CSV ─────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
REPO = HERE.parent / "target" / "visidata"
SAMPLE_CSV = REPO / "sample_data" / "benchmark.csv"

if not SAMPLE_CSV.exists():
    sys.exit(f"runner: sample CSV not found: {SAMPLE_CSV}")

# ── add the visidata package to sys.path if needed ────────────────────────
visidata_pkg = str(REPO)
if visidata_pkg not in sys.path:
    sys.path.insert(0, visidata_pkg)

# ── run visidata in-process ───────────────────────────────────────────────
with tempfile.NamedTemporaryFile(
    suffix=".tsv", prefix="first_light_out_", delete=False
) as tmp:
    out_path = tmp.name

try:
    from visidata.main import main_vd

    # Patch sys.argv to simulate: vd --batch <csv> -o <out>
    sys.argv = [
        "vd",
        "--batch",
        str(SAMPLE_CSV),
        "-o", out_path,
    ]

    try:
        rc = main_vd()
    except SystemExit as exc:
        rc = exc.code if exc.code is not None else 0

    sys.exit(int(rc) if isinstance(rc, int) else 0)

finally:
    # Clean up the temp output file
    try:
        os.unlink(out_path)
    except OSError:
        pass
