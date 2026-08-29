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

# Install coverage.py into the target's own virtual environment.
# (visidata example; adjust for a different target.)
.\target\visidata\.venv\Scripts\python.exe -m pip install coverage

# Run the full observation pass.
.\target\visidata\.venv\Scripts\python.exe first_light.py `
    --package .\target\visidata\visidata `
    --runner  .\runners\visidata_runner.py `
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

Measured against visidata 3.x with the batch-mode runner.

| Scope | Total | Observed in situ | Under driver | Never observed |
|-------|------:|------------------:|-------------:|---------------:|
| Product code | 2290 | 262 | 10 | 2018 |
| Whole package | 2791 | 265 | 10 | 2516 |

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
