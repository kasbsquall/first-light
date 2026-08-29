<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/first-light-mark-light.png">
  <img src="assets/first-light-mark-dark.png" alt="First Light" width="96">
</picture>

# First Light

First Light answers one question: which Python functions in a codebase have ever
been observed executing, and which have never run at all?

It instruments a real run of the target application under coverage.py, parses
every function with the AST, and classifies each one against the observed line
set.  The output is `evidence.json` (a machine-readable record of every unit and
its provenance) and `report.html` (a visual map of the entire function
population).

---


**[Read the evidence report](https://kasbsquall.github.io/first-light/)** for the
current measurement of visidata: which functions have been observed running,
which were reached only because an agent built something to reach them, and
which have no execution record at all.


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

# Install the target itself into its own virtual environment.
# (visidata example; adjust for a different target.)
.\target\visidata\.venv\Scripts\python.exe -m pip install -e target\visidata

# Runner-layer dependencies: coverage, pytest, and windows-curses.
.\target\visidata\.venv\Scripts\python.exe -m pip install -r requirements-runners.txt

# Run the full observation pass.  All three baselines run together: the
# program alone, the test suite, and the session-log replay each reach
# different functions; the figure depends on all three.
.\target\visidata\.venv\Scripts\python.exe first_light.py `
    --package .\target\visidata\visidata `
    --runner  .\runners\visidata_runner.py        --runner-id cli `
    --runner  .\runners\visidata_pytest_runner.py --runner-id test_suite `
    --runner  .\runners\visidata_replay_runner.py --runner-id replay `
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

First Light starts the observation range at `node.body[0].lineno`, the first
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

Measured against visidata 3.x, after three independent baselines: the program
run through its own CLI in batch mode; the project's own pytest suite (256 tests
collected, 252 passing, the 4 failures POSIX-specific, with the counts and the
non-zero exit code recorded rather than filtered out); and a replay of the 52 `.vdj`
command logs it ships, which exercises the program the way a person drove it.
The project also ships 112 `.vd` and 21 `.vdx` logs; this baseline replays the
`.vdj` set only, so the 144 functions it reaches alone are a floor, not a
ceiling.

| Scope | Total | Observed in situ | Under driver | Driver redundant | Never observed |
|-------|------:|------------------:|-------------:|-----------------:|---------------:|
| Product code | 2290 | 1082 | 2 | 6 | 1200 |
| Whole package | 2791 | 1151 | 2 | 6 | 1632 |

No baseline is a superset of another.  384 product functions are reached only
by the test suite, 144 only by the replayed sessions, and 1 only by the batch
CLI run; 298 by the replay and the suite together, 9 by the replay and the CLI,
and 252 by all three. Those figures cover the 1088 product functions a baseline
reached, which is the 1082 counted in situ plus the 6 whose driver a baseline
made redundant.  After all three, 1200 product functions have no
execution record at all.

The batch CLI run is now almost entirely subsumed: replaying real sessions
reaches nearly everything it did and 144 functions besides.  That is worth
stating rather than hiding, because it says the earlier claim to have "run the
program" rested on a single non-interactive invocation.

The 6 counted under 'driver redundant' are functions an agent had written a driver
for before the test-suite baseline existed.  The baseline reached them on
its own, so the driver became redundant.  Those units are marked as such
rather than deleted, and the report shows them as their own group.

Two further drivers were rejected, and both functions remain
`never_observed`.

The indirect call-site form has a limit worth stating. It exists for `stdev`,
which is reached through `self.funcValues(vals)` after being bound by
`vd.aggregator('stdev', stdev, ...)`. Its binding segment is checked by the same
rule as a direct call site, and a registration is not a call, so the case the
form was built for cannot be expressed in it. No driver uses the indirect form
today and none is tested. It is unfinished, not load-bearing.

One declared a call site the checker could not confirm: the
line it named is an indirect dispatch where the function's name never appears.
The other declared no call site at all.  A driver that declares nothing has
made no claim to verify, so the gate refuses it rather than recording the
function as reached on the strength of an assertion nobody made.

Closing that second case cost a promotion that had previously been counted.
The count is the thing that moved, not the standard.

Product code excludes the `tests`, `vendor`, `apps`, and `experimental`
directories.  Whole package includes them.

---

## How this was built

First Light was built with IBM Bob across twelve tasks.
[`bob_sessions/`](bob_sessions/README.md) holds the exported record of every one of them, as a readable Markdown transcript and as
JSON, with an index naming each task and what the record shows. That includes
the parts that do not flatter the process: Bob's first ten drivers copied
function bodies instead of importing them and marked ten functions as observed
on the strength of drivers that never called the real code, a human caught it, and the coverage gate that now
rejects that approach was built in response.

The index also states what the export does not cover, because commits made
after it was taken were corrections rather than Bob tasks.

---

### One function moves between runs

The figure is 1200 or 1201 depending on the run, and it is always the same
function: `visidata.shell.bytes_rstrip` at `shell.py:27`. The project's own test
suite reaches it on some runs and not others, so it lands in `observed_in_situ`
or in `never_observed` accordingly. Nothing else in the 2290 has moved across
the runs we have recorded.

We state it because a reader who runs the pipeline twice will see it, and
because a tool whose argument is that a claim must travel with its scope cannot
publish a figure that quietly shifts. One unit of run-to-run variance in a
population of 2290 does not change what the number says. Not saying so would.

---

## Testing the gate

The gate is the part of this tool that can be wrong in a way that matters, so it
has its own adversarial tests. They run on the standard library alone:

```
python tests/test_gate.py
```

Every case is either an attack an independent audit found and the gate accepted
at the time, or a legitimate call site that must keep passing so a tightening
does not quietly break the tool. The call-site rule was a substring test until an
audit promoted a function by citing the definition line of a different function
whose name contained it; six of the ten refused forms in that file are what
that rule used to accept; the shape check already caught the other four.

---

## Refusals are recorded, not printed and dropped

Every way a driver can fail the gate has a name. `RefusalClass` is a closed set
of thirteen: the body was never reached, the driver exited non-zero, coverage
export failed, no call site was declared, the declared call site does not parse
as `file:line`, the cited file does not exist, it lies outside the package, the
cited line is out of range, the line is not a call, the line is a definition or
an import or a comment rather than a use, and the line falls inside the
function's own body.

A refused unit carries `refusal_class` and `refusal_reason` in `evidence.json`,
so a refusal is as machine-readable as a promotion. `--refusal-report` measures
the distribution against a scratch copy and leaves the published evidence
untouched:

```
Drivers measured : 10
Promoted         : 2
Made redundant   : 6  (unit reached by a baseline)
Refused          : 2

Refusal class                        Count
--------------------------------------------
line_not_a_call_site                     1
no_call_site                             1
```

`report.html` renders this as its own section. The two refused drivers are
`visidata.aggregators.stdev`, whose declared call site names a line where
`self.funcValues(vals)` appears rather than a call to `stdev`, and
`visidata.path.vstat`, which declares no call site at all. Both remain
`never_observed`, and both record that a driver did reach them.

Ten drivers and two refusals measure nothing yet. What exists is the harness
that would, and a named class recorded against every refusal. The block above is
the output at the time of writing; adding a baseline moves it, so regenerate it
rather than trusting that it is still current.

---

## Where watsonx.ai fits

Driver generation is a model-in-a-loop step, and it is where watsonx.ai belongs.
This project already has the piece such a workload usually lacks: a verifier that
rejects generated code on evidence rather than on how it reads. That makes
`evidence.json` an evaluation harness, and the figure worth publishing is how
many of a model's drivers survive verification and how the refusals distribute.

It was attempted and not built. The event provisions IBM Bob per participant and
makes watsonx a separate, optional request for a team cloud account, which we did
not make. So authentication succeeds and the account lists twenty foundation
models including `ibm/granite-4-h-small`, but no watsonx.ai Runtime can be
attached to the project and inference returns
`no_associated_service_instance_error`. The captured responses are in
[docs/watsonx-attempt.md](docs/watsonx-attempt.md). The limit was the account we
were working in, and it was ours to request.

Nothing here calls watsonx.ai and no reported figure depends on a model having
been asked anything. Publishing an integration that does not run would be the
same unverified assertion this tool exists to detect.

---

## Hook wiring

`tools/fl_hook.py` is a Bob pre-edit advisory hook.  Before every file write,
Bob passes the target file path to the hook via stdin, and a line number when
the tool provides one.  Edit, Write and MultiEdit do not, so the hook reports
the file's state and says the edit could not be placed rather than naming a
function it cannot locate.  The hook
looks up the function unit that contains that line in `evidence.json` and prints
one advisory line describing the unit's provenance.  By default it exits 0 and never
blocks an edit.

To activate the hook, add the entry documented in `docs/hook-config.md` to
`~/.claude/hooks/hooks.json`.

### Turning the advisory into a gate

By default the hook reports and never blocks. Pass `--strict` in the hook command
(or set `FIRST_LIGHT_STRICT=1`) and an edit to a function with no execution
record exits 2, which stops the write.

It is off by default on purpose. A tool that blocks an edit the first time you
install it gets disabled the same afternoon, and a disabled hook reports nothing.
But an advisory that can never say no is a report rather than a control, and a
team that wants the control should not have to fork the file to get it. An
internal error in the hook never blocks either: not knowing whether an edit is
safe is not the same as knowing it is unsafe.

The `FIRST_LIGHT_EVIDENCE` environment variable overrides the evidence file
location when it is not adjacent to the files being edited.

---

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/daedalus-mark-light.png">
  <img src="assets/daedalus-mark-dark.png" alt="Daedalus" width="72">
</picture>

Built by **Daedalus** for the IBM TechXchange 2026 Pre-conference Dev Day
Hackathon, with IBM Bob.
