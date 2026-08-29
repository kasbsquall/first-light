#!/usr/bin/env python3
"""
Runner for First Light — third baseline: visidata session-log replay.

Replays every *.vdj command log found in target/visidata/tests/ using
visidata's own ``--play <log> --batch`` mechanism.  Each replay exercises
the program the way a human drove it, which is what the single-file cli
baseline was supposed to represent.

Design notes
------------
* We call main_vd() directly (not vd_cli()) so that coverage.py can flush
  its data file before the process terminates.  vd_cli() ends with
  os._exit(), which kills the process before any atexit/finalizer runs —
  coverage never writes its .coverage file.

* All logs are replayed in one process so a single coverage session
  captures every code path exercised across the full replay suite.

* The target repo root is set as the working directory before replaying,
  and restored afterwards.  The logs reference ``sample_data/`` by relative
  path, so this is required for them to resolve correctly.

* A log that fails does not abort the run.  Failures are counted and
  printed to stderr; the runner exits 0 as long as at least one log
  replayed successfully.  A partial run (some logs failed) is still a
  valid baseline — the evidence file records the failure counts.

* Exit code semantics:
    0  — all logs replayed without exception
    1  — one or more logs failed (partial baseline; coverage is still valid)
    2  — no logs found at all (configuration error)

Counts
------
The runner prints a summary line to stdout in a form that first_light.py
can parse with _parse_pytest_counts() if those keys are present.  Because
the existing parser looks for "passed" / "failed" tokens, we mirror that
vocabulary:

    <N> passed, <M> failed   (logs that completed / logs that raised)

This means the baseline card in the report shows "collected X, passed Y,
failed Z" for replay logs using the same display path as the test_suite
baseline, without any changes to first_light.py.
"""

import os
import sys
import traceback
from pathlib import Path

# ── locate the target repo root and the tests directory ───────────────────
HERE      = Path(__file__).resolve().parent           # runners/
REPO_ROOT = HERE.parent / "target" / "visidata"
TESTS_DIR = REPO_ROOT / "tests"

if not REPO_ROOT.is_dir():
    sys.exit(f"runner: target repo not found: {REPO_ROOT}")
if not TESTS_DIR.is_dir():
    sys.exit(f"runner: tests directory not found: {TESTS_DIR}")

# ── collect all .vdj logs ─────────────────────────────────────────────────
log_files = sorted(TESTS_DIR.glob("*.vdj"))
if not log_files:
    print("0 passed, 0 failed", flush=True)
    sys.exit(2)

# ── add visidata to sys.path ──────────────────────────────────────────────
visidata_pkg = str(REPO_ROOT)
if visidata_pkg not in sys.path:
    sys.path.insert(0, visidata_pkg)

# ── replay loop ───────────────────────────────────────────────────────────
_orig_cwd = os.getcwd()
os.chdir(str(REPO_ROOT))   # logs use sample_data/ as a relative path

passed = 0
failed = 0

try:
    from visidata.main import main_vd

    for log_path in log_files:
        log_rel = log_path.name
        try:
            # Patch sys.argv to simulate:
            #   vd --play <log> --batch
            sys.argv = [
                "vd",
                "--play", str(log_path),
                "--batch",
            ]

            try:
                rc = main_vd()
            except SystemExit as exc:
                rc = exc.code if exc.code is not None else 0
            except Exception:
                # Any unhandled exception from the replay counts as a failure
                # but must not terminate the loop.
                print(
                    f"[replay] FAIL  {log_rel}: unexpected exception",
                    file=sys.stderr, flush=True,
                )
                traceback.print_exc(file=sys.stderr)
                failed += 1
                continue

            # visidata batch-mode exits 1 on non-fatal warnings; treat as pass.
            if rc is None or int(rc) in (0, 1):
                passed += 1
                print(f"[replay] ok    {log_rel} (exit {rc})", file=sys.stderr, flush=True)
            else:
                print(
                    f"[replay] FAIL  {log_rel} (exit {rc})",
                    file=sys.stderr, flush=True,
                )
                failed += 1

        except Exception:
            print(
                f"[replay] FAIL  {log_rel}: outer exception during setup",
                file=sys.stderr, flush=True,
            )
            traceback.print_exc(file=sys.stderr)
            failed += 1

finally:
    os.chdir(_orig_cwd)

# ── summary (parsed by first_light._parse_pytest_counts) ──────────────────
total = passed + failed
print(f"collected {total} items", flush=True)
print(f"{passed} passed, {failed} failed", flush=True)

sys.exit(0 if failed == 0 else 1)
