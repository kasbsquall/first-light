# source: target/visidata/visidata/aggregators.py:147
# call site: target/visidata/visidata/features/ping.py:33 -- mean(r.getValues(col.sheet.source.rows))
# NOTE: windows-curses installed to satisfy curses dependency

import sys
sys.path.insert(0, 'target/visidata')

import visidata  # noqa: F401 — required to initialise the package
from visidata.aggregators import mean  # the REAL function

# 1. List of floats (typical ping latencies in ms)
# call site: target/visidata/visidata/features/ping.py:33 -- mean(r.getValues(col.sheet.source.rows))
result1 = mean([1.2, 5.6, 3.4, 2.1])
expected1 = (1.2 + 5.6 + 3.4 + 2.1) / 4
assert result1 == expected1, f"Test 1 failed: {result1} != {expected1}"
print(f"Test 1 PASS: mean([1.2, 5.6, 3.4, 2.1]) = {result1}")

# 2. Iterator input (as getValues may return an iterator)
result2 = mean(iter([10.0, 20.0]))
assert result2 == 15.0, f"Test 2 failed: {result2} != 15.0"
print(f"Test 2 PASS: mean(iter([10.0, 20.0])) = {result2}")

# 3. Empty input -> None (if-guard not entered, returns None implicitly)
result3 = mean([])
assert result3 is None, f"Test 3 failed: {result3} != None"
print(f"Test 3 PASS: mean([]) = {result3}")

# 4. Single value
result4 = mean([42.0])
assert result4 == 42.0, f"Test 4 failed: {result4} != 42.0"
print(f"Test 4 PASS: mean([42.0]) = {result4}")

print("SUCCESS")
