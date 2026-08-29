# Driver: colname_letters from target/visidata/visidata/utils.py
#
# NOTE: windows-curses installed to satisfy curses dependency
#
# call site: target/visidata/visidata/sheets.py:1086 -- colname_letters(self.colname_ctr)
#
# Real call sites:
#   target/visidata/visidata/sheets.py:1086  ->  colname_letters(self.colname_ctr)
#       colname_ctr is a 1-based int counter incremented per new column
#   target/visidata/visidata/sheets.py:1242  ->  colname_letters(i+1)
#       i is a 0-based enumerate index, so i+1 is 1-based

import sys
sys.path.insert(0, 'target/visidata')

import visidata
from visidata.utils import colname_letters  # the REAL function

# ── Test cases ────────────────────────────────────────────────────────────────
cases = [
    (1,  'A'),    # sheets.py:1086 colname_ctr starts at 1
    (2,  'B'),
    (26, 'Z'),
    (27, 'AA'),   # sheets.py:1242 i+1 for i=26
    (28, 'AB'),
    (52, 'AZ'),
    (53, 'BA'),
    (702, 'ZZ'),
    (0,  ''),
]

for num, expected in cases:
    result = colname_letters(num)
    assert result == expected, f"colname_letters({num}) -> {result!r}, expected {expected!r}"
    print(f"  colname_letters({num:>4}) = {result!r}  OK")

print("SUCCESS")
