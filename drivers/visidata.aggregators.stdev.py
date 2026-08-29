import sys
sys.path.insert(0, 'target/visidata')

# NOTE: windows-curses installed to satisfy curses dependency
import visidata

from visidata.aggregators import stdev  # the REAL function

# call site: target/visidata/visidata/aggregators.py:162 -- funcValues(vals) where funcValues=stdev
result = stdev([2.1, 5.6, 3.4, 7.2, 1.8])
print(f"stdev([2.1, 5.6, 3.4, 7.2, 1.8]) = {result}")
