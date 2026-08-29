# source: target/visidata/visidata/aggregators.py:169
# call site: target/visidata/visidata/aggregators.py:198 -- _percentile(sorted(col.getValues(rows)), self.pct/100, key=float)
# NOTE: windows-curses installed to satisfy curses dependency

import sys
sys.path.insert(0, 'target/visidata')

import visidata
from visidata.aggregators import _percentile

# Mirrors: _percentile(sorted(col.getValues(rows)), self.pct/100, key=float)
# call site: target/visidata/visidata/aggregators.py:198 -- _percentile(sorted(col.getValues(rows)), self.pct/100, key=float)

# Median of [1,2,3,4,5] at 50th percentile => 3.0
r1 = _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5, key=float)
assert r1 == 3.0, f"Expected 3.0, got {r1}"

# Interpolated case: 25th percentile of [10,20,30,40]
# k = 3 * 0.25 = 0.75; f=0, c=1; d0=10*(1-0.75)=2.5; d1=20*0.75=15; => 17.5
r2 = _percentile([10.0, 20.0, 30.0, 40.0], 0.25, key=float)
assert r2 == 17.5, f"Expected 17.5, got {r2}"

# Empty list returns None
r3 = _percentile([], 0.5, key=float)
assert r3 is None, f"Expected None, got {r3}"

# 0th percentile => first element
r4 = _percentile([1.0, 2.0, 3.0], 0.0, key=float)
assert r4 == 1.0, f"Expected 1.0, got {r4}"

# 100th percentile => last element
r5 = _percentile([1.0, 2.0, 3.0], 1.0, key=float)
assert r5 == 3.0, f"Expected 3.0, got {r5}"

print("SUCCESS")
