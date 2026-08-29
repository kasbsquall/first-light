"""
Driver: target/visidata/visidata/utils.py :: getattrdeep
=========================================================
Real call site:
  - target/visidata/visidata/column.py:546
      getattrdeep(row, col.expr, None)

NOTE: windows-curses installed to satisfy curses dependency
"""

import sys
sys.path.insert(0, 'target/visidata')

import visidata  # noqa: F401 -- ensure full package is initialised
from visidata.utils import getattrdeep  # the REAL function

# call site: target/visidata/visidata/column.py:546 -- getattrdeep(row, col.expr, None)


# ── Minimal fixture objects mirroring real call sites ─────────────────────────

class Row:
    """Mirrors a visidata row object with plain attributes."""
    def __init__(self, name, child=None):
        self.name = name
        self.child = child


class Child:
    def __init__(self, value):
        self.value = value


# ── Test cases ────────────────────────────────────────────────────────────────

def run_tests():
    passed = 0
    failed = 0

    def check(label, result, expected):
        nonlocal passed, failed
        if result == expected:
            print(f"  PASS  [{label}] => {result!r}")
            passed += 1
        else:
            print(f"  FAIL  [{label}] => {result!r}  (expected {expected!r})")
            failed += 1

    # Test 1: plain attr "name"  (mirrors column.py:546 with simple expr)
    row = Row("alice")
    result = getattrdeep(row, "name", None)
    check("plain attr 'name'", result, "alice")

    # Test 2: dotted path "child.value"  (mirrors column.py:546 dotted expr)
    row2 = Row("bob", child=Child(42))
    result2 = getattrdeep(row2, "child.value", None)
    check("dotted path 'child.value'", result2, 42)

    # Test 3: missing attr → default None  (mirrors both call sites)
    row3 = Row("carol")
    result3 = getattrdeep(row3, "nonexistent", None)
    check("missing attr -> default None", result3, None)

    # Test 4: missing dotted path → default None
    row4 = Row("dave")
    result4 = getattrdeep(row4, "child.value", None)   # child is None
    check("missing dotted path -> default None", result4, None)

    print()
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


# ── Entry point ───────────────────────────────────────────────────────────────

ok = run_tests()
sys.exit(0 if ok else 1)
