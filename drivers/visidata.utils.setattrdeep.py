import sys
sys.path.insert(0, 'target/visidata')

# NOTE: windows-curses installed to satisfy curses dependency
import visidata  # noqa: F401  (needed to satisfy visidata package imports)

from visidata.utils import setattrdeep  # the REAL function

# --- Test 1: call site mirror of column.py:551 ---
# call site: target/visidata/visidata/column.py:551 -- setattrdeep(row, self.expr, val)
# row is a plain Python object with an existing attr; overwrite it
Row = type('Row', (), {'name': 'Alice'})()
setattrdeep(Row, 'name', 'Bob')
assert Row.name == 'Bob', f"Expected 'Bob', got {Row.name!r}"
print(f"Test 1 PASS: Row.name = {Row.name!r}  (overwrite existing attr)")

# --- Test 2: set a brand-new (non-dotted) attr on the row ---
setattrdeep(Row, 'age', 30)
assert Row.age == 30, f"Expected 30, got {Row.age!r}"
print(f"Test 2 PASS: Row.age = {Row.age!r}  (new attr, setter path)")

# --- Test 3: non-str attr (int key) — exercises the not-isinstance branch ---
d = {}
setattrdeep(d, 0, 'zero', getter=lambda o, k: o[k], setter=lambda o, k, v: o.__setitem__(k, v))
assert d[0] == 'zero', f"Expected 'zero', got {d[0]!r}"
print(f"Test 3 PASS: d[0] = {d[0]!r}  (non-str attr path)")

print("SUCCESS")
