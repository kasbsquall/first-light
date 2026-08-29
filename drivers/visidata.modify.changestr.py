import sys
sys.path.insert(0, 'target/visidata')

# NOTE: windows-curses installed to satisfy curses dependency
import visidata
import visidata.modify  # applies @Sheet.api decorator that attaches changestr to Sheet
from visidata import Sheet

# Create a real Sheet instance
sheet = Sheet('test')
sheet.rowtype = 'rows'

# Mock data matching real call-site shapes exactly
# adds    = {rowid: row}
# mods    = {rowid: (row, {col: val, ...})}
# deletes = {rowid: row}
adds    = {1: object(), 2: object()}
mods    = {3: (object(), {'col_a': 'v1', 'col_b': 'v2'}),
           4: (object(), {'col_c': 'v3'})}
deletes = {5: object()}

# call site: target/visidata/visidata/modify.py:345 -- sheet.changestr(adds, mods, deletes)
result = sheet.changestr(adds, mods, deletes)
print('REACHED')
print('result:', repr(result))
assert result == 'add 2 rows and change 3 values and delete 1 rows', \
    'mismatch: ' + repr(result)

# Additional cases to cover all branches
result2 = sheet.changestr(adds, {}, {})
assert result2 == 'add 2 rows', repr(result2)

result3 = sheet.changestr({}, mods, {})
assert result3 == 'change 3 values', repr(result3)

result4 = sheet.changestr({}, {}, deletes)
assert result4 == 'delete 1 rows', repr(result4)

result5 = sheet.changestr({}, {}, {})
assert result5 == '', repr(result5)

print('SUCCESS')
