# First Light

First Light answers one question: which Python functions in a codebase have ever
been observed executing, and which have never run at all?

It instruments a real run of the target application under coverage.py, parses
every function with the AST, and classifies each one against the observed line
set.  The output is `evidence.json` (a machine-readable record of every unit and
its provenance) and `report.html` (a visual map of the entire function
population).

---

## Quick start from a clean clone

```
git clone <this-repo>
cd first-light

# The target is not vendored.  Obtain it at the revision these figures were
# measured against, or the numbers you get will not be the numbers published
# here.
git clone https://github.com/saulpw/visidata.git target/visidata
git -C target/visidata checkout 1d8a6fcd7f031a140c5662943af868e9108343ed
py -m venv target/visidata/.venv

# Install coverage.py into the target's own virtual environment.
# (visidata example; adjust for a different target.)
.\target\visidata\.venv\Scripts\python.exe -m pip install -e target\visidata

# Both baselines run in one pass.  The headline figure depends on running
# both: the program alone and the test suite alone reach different
# functions, and neither is a superset of the other.
.\target\visidata\.venv\Scripts\python.exe -m pip install -r requirements-runners.txt

# Run the full observation pass.
.\target\visidata\.venv\Scripts\python.exe first_light.py `
    --package .\target\visidata\visidata `
    --runner  .\runners\visidata_runner.py        --runner-id cli `
    --runner  .\runners\visidata_pytest_runner.py --runner-id test_suite `
    --python  .\target\visidata\.venv\Scripts\python.exe `
    --evidence evidence.json `
    --report  report.html

# Promote drivers for functions never reached by the runner alone.
.\target\visidata\.venv\Scripts\python.exe first_light.py `
    --promote-driver --all `
    --evidence evidence.json

# Regenerate the report after promotion.
.\target\visidata\.venv\Scripts\python.exe first_light.py `
    --report report.html `
    --evidence evidence.json
```

`evidence.json` is committed at the repository root.  Every number in
`report.html` is derived from it.

---

## Provenance levels

Each function unit in `evidence.json` carries one of three provenance values.

### `observed_in_situ`

The function executed during a normal, unassisted run of the application.  No
special scaffolding was needed.  This is the strongest signal: the code path is
reachable under real operating conditions.

### `observed_under_driver`

The function only ran because a purpose-built driver script called it directly.
This means it is reachable in principle but was never triggered by the
application's own logic during the observation window.  Driver files live in
`drivers/` and are named after the qualified function they target (e.g.
`visidata.utils.moveListItem.py`).

### `never_observed`

Nothing that ran during either the normal observation pass or any driver ever
entered this function's body.  It may be dead code, may require conditions the
runner does not exercise, or may be reachable only through an interaction path
not covered by the runner.

---

## The body[0].lineno rule

Coverage.py records which source lines were executed.  Every `def` statement
executes at module import time, because Python runs the `def` line to bind the
function object to the name.  Starting the observation range at `node.lineno`
(the `def` line) would therefore mark every imported function as observed and
inflate the result toward 100%.

First Light starts the observation range at `node.body[0].lineno` — the first
line of the function's body, which only runs if the function is actually called.
This is the line recorded as `body_start` in `evidence.json`.

---

## The unit key and the property collision problem

Units are keyed in `evidence.json` by:

```
<absolute_file_path>::<qualified_name>#<def_line>
```

The `#<def_line>` suffix is not decorative.  A `@property` getter and its
`@setter` share exactly the same file path and qualified name (`module.Class.x`
in both cases).  Without the definition line number, writing the setter entry
would silently overwrite the getter entry, producing a unit count that is wrong
by exactly the number of property pairs.  Including `def_line` makes the key
unique for every function definition in the codebase, regardless of naming
collisions.

---

## Current figures

Measured against visidata 3.x, after two independent baselines: the program
run through its own CLI, and the project's own pytest suite (256 tests
collected, 252 passing; the 4 failures are POSIX-specific, and the counts
and the non-zero exit code are recorded in the evidence file rather than
filtered out).

| Scope | Total | Observed in situ | Under driver | Driver redundant | Never observed |
|-------|------:|------------------:|-------------:|-----------------:|---------------:|
| Product code | 2290 | 939 | 3 | 5 | 1343 |
| Whole package | 2791 | 1007 | 3 | 5 | 1776 |

Neither baseline is a superset of the other.  The test suite reaches 682
product functions the running program never touches; the running program
reaches 10 the test suite never touches; 252 are reached by both.  After
both, 1343 product functions have no execution record at all.

The 5 counted under 'driver redundant' are functions an agent had written a driver
for before the test-suite baseline existed.  The baseline reached them on
its own, so the driver became redundant.  Those units are marked as such
rather than deleted, and the report shows them as their own group.

Two further drivers were rejected, and both functions remain
`never_observed`.  One declared a call site the checker could not confirm: the
line it named is an indirect dispatch where the function's name never appears.
The other declared no call site at all.  A driver that declares nothing has
made no claim to verify, so the gate refuses it rather than recording the
function as reached on the strength of an assertion nobody made.

Closing that second case cost a promotion that had previously been counted.
The count is the thing that moved, not the standard.

Product code excludes the `tests`, `vendor`, `apps`, and `experimental`
directories.  Whole package includes them.

---

## Hook wiring

`tools/fl_hook.py` is a Bob pre-edit advisory hook.  Before every file write,
Bob passes the target file path and line number to the hook via stdin.  The hook
looks up the function unit that contains that line in `evidence.json` and prints
one advisory line describing the unit's provenance.  It always exits 0 and never
blocks an edit.

To activate the hook, add the entry documented in `docs/hook-config.md` to
`~/.claude/hooks/hooks.json`.

The `FIRST_LIGHT_EVIDENCE` environment variable overrides the evidence file
location when it is not adjacent to the files being edited.
