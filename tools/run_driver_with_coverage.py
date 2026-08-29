#!/usr/bin/env python3
"""
run_driver_with_coverage.py
===========================
Run one driver script under coverage.py and verify that lines inside
the TARGET function's body in the real source file were actually executed.

Usage:
    python tools/run_driver_with_coverage.py \\
        --driver   drivers/visidata.utils.moveListItem.py \\
        --src-file target/visidata/visidata/utils.py \\
        --body-start 64 \\
        --body-end   69

Exit codes:
    0  -- body lines confirmed executed (REACHED)
    1  -- driver ran but zero body lines hit (NOT REACHED)
    2  -- driver crashed (NOT REACHED, with error)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--driver",     required=True, help="driver script to run")
    parser.add_argument("--src-file",   required=True, help="real source file containing target function")
    parser.add_argument("--body-start", required=True, type=int, help="first line of function body (body_start from evidence.json)")
    parser.add_argument("--body-end",   required=True, type=int, help="last line of function body (body_end from evidence.json)")
    args = parser.parse_args(argv)

    driver_abs  = str(Path(args.driver).resolve())
    src_abs     = str(Path(args.src_file).resolve())
    pkg_abs     = str(Path("target/visidata/visidata").resolve())
    body_lines  = set(range(args.body_start, args.body_end + 1))

    with tempfile.TemporaryDirectory(prefix="fl_drv_cov_") as td:
        data_file = os.path.join(td, ".coverage")
        json_out  = os.path.join(td, "cov.json")

        # --- run driver under coverage ---
        run_result = subprocess.run(
            [sys.executable, "-m", "coverage", "run",
             f"--source={pkg_abs}",
             f"--data-file={data_file}",
             driver_abs],
            capture_output=True, text=True, timeout=30,
            cwd=str(Path(".").resolve()),
        )

        print("=== driver stdout ===")
        print(run_result.stdout)
        if run_result.stderr:
            print("=== driver stderr ===")
            print(run_result.stderr[:1000])

        if run_result.returncode not in (0, 1):
            print(f"[coverage] driver exited {run_result.returncode} -- treating as crash")
            return 2

        # --- export coverage to JSON ---
        exp_result = subprocess.run(
            [sys.executable, "-m", "coverage", "json",
             f"--data-file={data_file}",
             "-o", json_out],
            capture_output=True, text=True, timeout=15,
        )
        if exp_result.returncode != 0:
            print("[coverage] json export failed:", exp_result.stderr[:300])
            return 2

        with open(json_out) as fh:
            cov = json.load(fh)

        # --- check body lines ---
        hit_lines: set[int] = set()
        for file_path, file_data in cov.get("files", {}).items():
            if Path(file_path).resolve() == Path(src_abs).resolve():
                executed = set(file_data.get("executed_lines", []))
                hit_lines = executed & body_lines
                break

        if hit_lines:
            print(f"\n[coverage] CONFIRMED: lines {sorted(hit_lines)} in {args.src_file} executed (body_start={args.body_start}, body_end={args.body_end})")
            return 0
        else:
            print(f"\n[coverage] NOT REACHED: zero body lines ({args.body_start}-{args.body_end}) executed in {args.src_file}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
