#!/usr/bin/env python3
"""
Runner for First Light — second baseline: visidata's own test suite.

Runs `python -m pytest visidata/tests` from the target repo working directory,
collecting coverage over the visidata package.

Design notes
------------
* We call pytest.main() directly (not subprocess.run) so that coverage.py can
  observe every line executed inside the visidata package during the test run.
  Using subprocess.run would fork a child process that coverage.py's outer
  `coverage run` wrapper cannot instrument.

* The test suite is run with --tb=short -q to keep output compact; the pytest
  exit code is forwarded as our own exit code so the baseline entry records
  whether the run was partial.

* We do NOT filter or skip any tests.  Some tests fail on Windows (e.g.
  test_editor tests need a POSIX #!/bin/sh editor script; test_path.py asserts
  POSIX path behaviour).  Their failures are real and are recorded verbatim in
  the baseline exit code.  A partial run is still a valid baseline, as long as
  the evidence says so.

* Exit code semantics:
    0  — all tests passed
    1  — some tests failed (expected on Windows; coverage is still valid)
    2+ — pytest infrastructure error (unexpected)

Environment
-----------
pytest is already installed in the target venv.  See requirements-runners.txt
for the pinned version.

Working directory
-----------------
The runner changes os.chdir to the target repo root before invoking pytest so
that relative paths in test fixtures resolve correctly.  It restores the
original cwd afterwards.
"""

import os
import sys
from pathlib import Path

# ── locate the target repo root ────────────────────────────────────────────
HERE = Path(__file__).resolve().parent          # runners/
REPO_ROOT = HERE.parent / "target" / "visidata"

if not REPO_ROOT.is_dir():
    sys.exit(f"runner: target repo not found: {REPO_ROOT}")

# The visidata package directory (what First Light told coverage to watch).
VISIDATA_PKG = REPO_ROOT / "visidata"
if not VISIDATA_PKG.is_dir():
    sys.exit(f"runner: visidata package not found: {VISIDATA_PKG}")

# ── ensure pytest is importable ────────────────────────────────────────────
try:
    import pytest
except ImportError:
    sys.exit("runner: pytest not found — install it via: pip install pytest==9.1.1")

# ── change to the target repo root so fixture paths resolve ────────────────
_orig_cwd = os.getcwd()
os.chdir(str(REPO_ROOT))

try:
    rc = pytest.main([
        "visidata/tests",     # relative to cwd = REPO_ROOT
        "-q",
        "--tb=short",
        "--no-header",
    ])
finally:
    os.chdir(_orig_cwd)

# Declare the unit so the evidence records what was counted rather than leaving
# a reader to infer it from the baseline id.
print("first-light-unit: tests", flush=True)

# pytest.main returns an ExitCode enum; cast to int for sys.exit.
sys.exit(int(rc))
