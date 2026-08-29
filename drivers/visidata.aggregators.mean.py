# source: target/visidata/visidata/aggregators.py:147
# NO VERIFIABLE CALL SITE. This driver previously cited features/ping.py:33,
# which reads mean(r.getValues(...)) and looks right. It is not: line 6 of
# that file is 'from statistics import mean', so the call goes to the
# standard library and the citation was false. visidata's own mean is bound
# by vd.aggregator('mean', mean, ...) and reached through dispatch, and this
# gate does not accept a registration as a call. The claim is withdrawn
# rather than restated, so this driver is refused for declaring nothing.
# NOTE: windows-curses installed to satisfy curses dependency

import sys
sys.path.insert(0, 'target/visidata')

import visidata  # noqa: F401 -- required to initialise the package
from visidata.aggregators import mean  # the REAL function

# 1. List of floats (typical ping latencies in ms)
# NO VERIFIABLE CALL SITE. This driver previously cited features/ping.py:33,
# which reads mean(r.getValues(...)) and looks right. It is not: line 6 of
# that file is 'from statistics import mean', so the call goes to the
# standard library and the citation was false. visidata's own mean is bound
# by vd.aggregator('mean', mean, ...) and reached through dispatch, and this
# gate does not accept a registration as a call. The claim is withdrawn
# rather than restated, so this driver is refused for declaring nothing.
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
