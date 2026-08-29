#!/usr/bin/env python3
"""Adversarial tests for the promotion gate.

This file exists because a tool whose product is verification had none of its
own. Every case below is an attack that was found by an independent audit and
accepted by the gate at the time, or a legitimate case that must keep passing so
a tightening does not quietly break the tool.

Runs on the standard library alone, like first_light.py itself:

    python tests/test_gate.py

Exit code 0 if every case behaves as recorded, 1 otherwise.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("first_light", ROOT / "first_light.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FL = _load()

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print("  [OK]   %s" % name)
    else:
        FAILED += 1
        print("  [FAIL] %s %s" % (name, detail))


def in_temp_file(source: str):
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    Path(path).write_text(source, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. The call-site rule must accept a call and nothing else.
#
# The rule was a substring test until an audit cited "def _recursive_bezier" to
# promote "bezier". Six forms mention the name without calling it, and every one
# of them satisfied the old rule.
# ---------------------------------------------------------------------------
NOT_CALLS = [
    ("definition of another function", "def _recursive_bezier(x1, y1):\n    pass\n"),
    ("the function's own definition",  "def bezier(x1, y1):\n    pass\n"),
    ("alias assignment",               "alias = bezier\n"),
    ("registration by name",           "vd.addCommand(None, 'bezier', 1)\n"),
    ("cache dict named after it",      "bezier_cache = {}\n"),
    ("type annotation",                "x: bezier = None\n"),
    ("del statement",                  "del bezier\n"),
    ("import",                         "from visidata.bezier import bezier\n"),
    ("__all__ entry",                  "__all__ = ['bezier']\n"),
    ("comment",                        "# bezier(1, 2)\n"),
]

REAL_CALLS = [
    ("bare call",            "pts = bezier(1, 2, 3, 4, 5, 6)\n"),
    ("attribute call",       "pts = shape.bezier(1, 2)\n"),
    ("call inside a call",   "pts = list(bezier(1, 2))\n"),
]


def test_call_site_rule() -> None:
    print("\n--- the cited line must be a call ---")
    for label, src in NOT_CALLS:
        p = in_temp_file(src)
        reason = FL._line_calls(p, 1, "bezier")
        check("refuses: %s" % label, reason is not None,
              "-> accepted, which it must not be")
        os.unlink(p)
    for label, src in REAL_CALLS:
        p = in_temp_file(src)
        reason = FL._line_calls(p, 1, "bezier")
        check("accepts: %s" % label, reason is None, "-> %s" % reason)
        os.unlink(p)
    # A file that will not parse is refused, not waved through: an unparseable
    # claim is not a verified one either.
    p = in_temp_file("def broken(:\n")
    check("refuses: a file that does not parse",
          FL._line_calls(p, 1, "bezier") is not None)
    os.unlink(p)


# ---------------------------------------------------------------------------
# 2. Shape rules that run before the call check.
# ---------------------------------------------------------------------------
def test_shape_rule() -> None:
    print("\n--- lines that cannot be a call site at all ---")
    cases = [
        ("comment",      "# bezier(1, 2)", True),
        ("definition",   "def bezier(x):", True),
        ("decorator",    "@property", True),
        ("import",       "import bezier", True),
        ("__all__",      "__all__ = ['bezier']", True),
        ("a real call",  "y = bezier(1, 2)", False),
    ]
    for label, line, should_refuse in cases:
        got = FL._is_not_a_call_site(line, "bezier")
        check("%s: %s" % ("refused" if should_refuse else "allowed", label),
              bool(got) == should_refuse, "-> %r" % got)


# ---------------------------------------------------------------------------
# 3. The observation rule: a function counts as observed only when a line in its
#    BODY ran. The def line executes on import and would inflate the figure from
#    roughly half the population to nearly all of it.
# ---------------------------------------------------------------------------
def test_body_not_def_line() -> None:
    print("\n--- observation starts at the body, not the def line ---")
    src = (
        "import math\n"
        "\n"
        "def never_called(a):\n"
        "    return a + 1\n"
    )
    p = Path(in_temp_file(src))
    funcs = FL.iter_functions(p, p.parent)
    target = [f for f in funcs if f.qualified_name.endswith("never_called")]
    check("the function is found", len(target) == 1, "-> %d found" % len(funcs))
    if target:
        fn = target[0]
        check("body starts after the def line", fn.body_start > fn.def_line,
              "-> def %d, body %d" % (fn.def_line, fn.body_start))
        check("the body range excludes the def line",
              fn.def_line not in range(fn.body_start, fn.body_end + 1))
    os.unlink(p)


# ---------------------------------------------------------------------------
# 4. The refusal taxonomy has to be a closed set with distinct values, or
#    recording a class says nothing.
# ---------------------------------------------------------------------------
def test_refusal_classes() -> None:
    print("\n--- refusal classes ---")
    values = [c.value for c in FL.RefusalClass]
    check("every class has a distinct value", len(values) == len(set(values)))
    check("there is a class for a missing call site", "no_call_site" in values)
    check("there is a class for a line that is not a call",
          "line_not_a_call_site" in values)
    check("there is a class for a call site outside the package",
          "call_site_outside_package" in values)


def main() -> int:
    print("first_light gate tests")
    test_call_site_rule()
    test_shape_rule()
    test_body_not_def_line()
    test_refusal_classes()
    print("\n%d passed, %d failed" % (PASSED, FAILED))
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
