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
# promote "bezier". All ten forms below mention the name without calling it, so
# all ten satisfied that rule. The shape check in section 2 already caught four
# of them; the other six needed the AST rewrite.
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


# ---------------------------------------------------------------------------
# 5. The hook's --strict mode, which is a second gate.
#
# It shipped deciding whether to block by looking for the substring
# "never observed" in the advisory it had just printed. The not-located message
# ends "N never observed", so it blocked every edit to any file holding more
# than one function: 229 of 250 in this target. Same defect as the promotion
# gate had, opposite direction. A gate decides on a value, never on prose.
# ---------------------------------------------------------------------------
def test_strict_gate() -> None:
    print(chr(10) + "--- the hook's strict mode ---")
    import json as _json
    import subprocess as _sub

    hook = str(ROOT / "tools" / "fl_hook.py")
    ev = ROOT / "evidence.json"
    if not ev.exists():
        check("evidence.json is present", False, "-> cannot exercise the hook")
        return
    units = _json.loads(ev.read_text(encoding="utf-8"))["units"]

    def first(prov):
        for u in units.values():
            if u.get("provenance") == prov:
                return u
        return None

    never, seen = first("never_observed"), first("observed_in_situ")

    def run(payload):
        r = _sub.run([sys.executable, hook, "--strict"],
                     input=_json.dumps(payload), capture_output=True, text=True)
        return r.returncode

    cases = [
        ("an edit with no line number does not block", 0,
         {"tool_name": "Edit",
          "tool_input": {"file_path": seen["file"], "old_string": "x"}}),
        ("a never-observed function blocks", 2,
         {"tool_input": {"file_path": never["file"], "line": never["body_start"]}}),
        ("an observed function does not block", 0,
         {"tool_input": {"file_path": seen["file"], "line": seen["body_start"]}}),
        ("an untracked file does not block", 0,
         {"tool_input": {"file_path": "/no/such/file.py", "line": 1}}),
        ("an empty payload does not block", 0, {}),
    ]
    for label, want, payload in cases:
        got = run(payload)
        check(label, got == want, "-> exit %d, wanted %d" % (got, want))

    # Without the flag nothing blocks, whatever the provenance.
    r = _sub.run([sys.executable, hook],
                 input=_json.dumps({"tool_input": {"file_path": never["file"],
                                                   "line": never["body_start"]}}),
                 capture_output=True, text=True)
    check("without --strict a never-observed edit does not block", r.returncode == 0,
          "-> exit %d" % r.returncode)


# ---------------------------------------------------------------------------
# 6. A call by the right name is not necessarily a call to the right function.
#
# A shipped driver cited features/ping.py:33, which reads mean(...) and looks
# correct. Line 6 of that file is "from statistics import mean", so the call
# goes to the standard library. The gate accepted it.
# ---------------------------------------------------------------------------
def test_citation_belongs_to_the_function() -> None:
    print(chr(10) + "--- the cited file must be able to see the function ---")
    base = ROOT / "target" / "visidata" / "visidata"
    if not base.is_dir():
        check("the target is present", False, "-> skipping")
        return
    cases = [
        ("a file that imports something else of the same name",
         base / "features" / "ping.py", base / "aggregators.py", "mean", True),
        ("the file that defines it",
         base / "aggregators.py", base / "aggregators.py", "_percentile", False),
        ("a file that imports the defining module",
         base / "column.py", base / "utils.py", "getattrdeep", False),
    ]
    for label, cited, defining, name, should_refuse in cases:
        got = FL._cites_the_right_function(str(cited), str(defining), name)
        check("%s: %s" % ("refused" if should_refuse else "accepted", label),
              bool(got) == should_refuse, "-> %r" % got)


# ---------------------------------------------------------------------------
# 7. The hook must work on a clone that is not the one that produced the
#    evidence. It matched absolute paths, so --strict found no unit anywhere
#    else and failed open: a gate silently disabled on every machine but one.
# ---------------------------------------------------------------------------
def test_hook_is_portable() -> None:
    print(chr(10) + "--- the hook works on another clone ---")
    import json as _json, shutil as _sh, subprocess as _sub, tempfile as _tf

    ev = ROOT / "evidence.json"
    if not ev.exists():
        check("evidence.json is present", False, "-> skipping")
        return
    units = _json.loads(ev.read_text(encoding="utf-8"))["units"]
    never = next((u for u in units.values()
                  if u.get("provenance") == "never_observed"), None)
    if not never or not Path(never["file"]).exists():
        check("a never-observed unit is available", False)
        return

    root = _tf.mkdtemp(prefix="fl_clone_")
    try:
        _sh.copy(str(ev), os.path.join(root, "evidence.json"))
        tail = never["file"].replace(os.sep, "/").split("/visidata/visidata/")[-1]
        dst = os.path.join(root, "target", "visidata", "visidata", *tail.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _sh.copy(never["file"], dst)
        payload = _json.dumps({"tool_input": {"file_path": dst,
                                              "line": never["body_start"]}})
        r = _sub.run([sys.executable, str(ROOT / "tools" / "fl_hook.py"), "--strict"],
                     input=payload, capture_output=True, text=True, cwd=root)
        check("a never-observed edit still blocks on another clone",
              r.returncode == 2, "-> exit %d" % r.returncode)
    finally:
        _sh.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. The receiver rule. An audit promoted StoredList.append by citing
#    stored_list.py:34, which is `ret.append(value)` on a plain Python list; the
#    line below it in visidata's own source reads "replace without using
#    .append". Neither existing check could catch it: the import guard
#    short-circuits when the cited file is the defining file, and the shape
#    check compares the attribute name and nothing else. Both of those are
#    asserted below, because they are the reason a third check exists.
#
#    The rule has to close that without refusing real dispatch.
#    `sheet.changestr(...)` is also an attribute call in the defining file, but
#    changestr carries @Sheet.api, so visidata binds it to the class at import
#    time and any instance is a legitimate receiver. Refusing it would have cost
#    a published promotion.
# ---------------------------------------------------------------------------
def test_receiver_rule() -> None:
    print(chr(10) + "--- the receiver of an attribute call ---")
    base = ROOT / "target" / "visidata" / "visidata"
    src = base / "stored_list.py"
    if not src.is_file():
        check("the target is present", False, "-> skipping")
        return

    line = src.read_text(encoding="utf-8", errors="replace").splitlines()[33]
    check("stored_list.py:34 is still ret.append(value)",
          "ret.append(value)" in line, "-> %r" % line.strip())

    check("the import guard alone would let it through",
          FL._cites_the_right_function(str(src), str(src), "append") is None)
    check("the shape check alone would let it through",
          FL._line_calls(str(src), 34, "append") is None)

    check("the receiver rule refuses it",
          FL._receiver_is_plausible(str(src), 34, "append", str(src)) is not None)

    # The same shape, resurrected by a nested def. The first version of the rule
    # only looked at module level and class bodies, so a closure came back as
    # "not defined here" and the rule switched itself off for 71 units. An audit
    # found this one live: adds is a plain dict.
    sq = base / "loaders" / "sqlite.py"
    if sq.is_file():
        nested_line = sq.read_text(encoding="utf-8", errors="replace").splitlines()[182]
        check("sqlite.py:183 is still for r in adds.values()",
              "adds.values()" in nested_line, "-> %r" % nested_line.strip())
        check("a nested def is found, not reported as absent",
              FL._defines_how(str(sq), "values") is not None)
        check("the receiver rule refuses an attribute call on a nested def",
              FL._receiver_is_plausible(str(sq), 183, "values", str(sq)) is not None)

    # Every call site this repository actually publishes must survive the rule,
    # including the one attribute call among them.
    real = [
        ("sheet.changestr, attached with @Sheet.api",
         base / "modify.py", 345, "changestr", base / "modify.py"),
        ("_percentile", base / "aggregators.py", 198, "_percentile", base / "aggregators.py"),
        ("getattrdeep", base / "column.py", 546, "getattrdeep", base / "utils.py"),
        ("setattrdeep", base / "column.py", 551, "setattrdeep", base / "utils.py"),
        ("moveListItem", base / "features" / "slide.py", 22, "moveListItem", base / "utils.py"),
        ("colname_letters", base / "sheets.py", 1086, "colname_letters", base / "utils.py"),
        ("modtime", base / "_urlcache.py", 18, "modtime", base / "path.py"),
    ]
    for label, cited, line_no, name, defining in real:
        got = FL._receiver_is_plausible(str(cited), line_no, name, str(defining))
        check("still accepted: %s" % label, got is None, "-> %r" % got)


def main() -> int:
    print("first_light gate tests")
    test_call_site_rule()
    test_shape_rule()
    test_body_not_def_line()
    test_refusal_classes()
    test_strict_gate()
    test_citation_belongs_to_the_function()
    test_hook_is_portable()
    test_receiver_rule()
    print("\n%d passed, %d failed" % (PASSED, FAILED))
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
