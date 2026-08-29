# NOTE: windows-curses installed to satisfy curses dependency
"""
Driver for: visidata.utils.moveListItem
Real source: target/visidata/visidata/utils.py  lines 63-69

Call sites (AST scan):
  target/visidata/visidata/features/slide.py:22  -- moveListItem(sheet.rows, rowidx, newcolidx)
  target/visidata/visidata/features/slide.py:60  -- moveListItem(sheet.columns, fromColIdx, toColIdx)
"""

import sys
sys.path.insert(0, 'target/visidata')

import visidata  # required to fully load the module

from visidata.utils import moveListItem  # import the REAL function from the REAL module

# call site: target/visidata/visidata/features/slide.py:22 -- moveListItem(sheet.rows, rowidx, newcolidx)
rows = ["row0", "row1", "row2", "row3", "row4"]
result_idx = moveListItem(rows, 1, 3)
print("REACHED")
assert result_idx == 3, f"Expected 3, got {result_idx}"
assert rows == ["row0", "row2", "row3", "row1", "row4"], f"Unexpected rows: {rows}"

# call site: target/visidata/visidata/features/slide.py:60 -- moveListItem(sheet.columns, fromColIdx, toColIdx)
cols = ["colA", "colB", "colC", "colD", "colE"]
result_idx2 = moveListItem(cols, 3, 1)
assert result_idx2 == 1, f"Expected 1, got {result_idx2}"
assert cols == ["colA", "colD", "colB", "colC", "colE"], f"Unexpected cols: {cols}"

# boundary clamping — toidx beyond list length
items = ["x", "y", "z"]
result_idx3 = moveListItem(items, 0, 99)
assert result_idx3 == 2, f"Expected clamped 2, got {result_idx3}"
assert items == ["y", "z", "x"], f"Unexpected items: {items}"

print("SUCCESS")
