import sys
import os

sys.path.insert(0, 'target/visidata')

# NOTE: windows-curses installed to satisfy curses dependency
import visidata
from visidata.path import vstat  # the REAL function

# note: 0 direct call sites in AST scan; function still defined in module; called directly by driver

# Use an absolute path so lru_cache sees a fresh argument it has never seen before.
# This guarantees the function body executes rather than returning a cached result.
evidence_abs = os.path.abspath('evidence.json')

# Test 1: existing file -> os.stat_result
result = vstat(evidence_abs)
assert result is not None, f"Expected stat result for existing file, got None (path={evidence_abs})"
assert hasattr(result, 'st_size'), "Expected st_size attribute on stat result"
print(f"  st_size = {result.st_size}")

# Test 2: nonexistent path -> None
result2 = vstat(evidence_abs + '.nonexistent_sentinel_xyz')
assert result2 is None, f"Expected None for nonexistent path, got {result2}"
print("  nonexistent path -> None (correct)")

print("SUCCESS")
