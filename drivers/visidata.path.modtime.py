# source: target/visidata/visidata/path.py:84
# call site: target/visidata/visidata/_urlcache.py:18 -- modtime(p) where p is a visidata Path
# NOTE: windows-curses installed to satisfy curses dependency

import sys
sys.path.insert(0, 'target/visidata')

import visidata
from visidata import Path
from visidata.path import modtime  # the REAL function

# Create a visidata Path (NOT pathlib.Path) pointing to a real file
p = Path('evidence.json')

# call site: target/visidata/visidata/_urlcache.py:18 -- modtime(p) where p is a visidata Path
result = modtime(p)
print(f"modtime result: {result!r}")
assert isinstance(result, float), f"Expected float, got {type(result)}"
print("SUCCESS")
