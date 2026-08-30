# First Light: problem and solution statement

Demo: https://youtu.be/GWdGS7IGTx0 (2:50)
Evidence report: https://kasbsquall.github.io/first-light/report.html

## The problem

Agents write code faster than anyone can read it and treat every function the
same. To an editing agent, a function exercised by a passing test and one that
has never executed look identical. Coverage does not close that gap: it reports
that a line was touched, not whether anyone watched the function run.

We measured visidata, a widely used Python tool. Of its 2290 product functions,
1200 have never been observed executing. That holds after running the program in
batch mode, after its own 256-test suite, and after replaying the 52 session
logs in its test suite. No baseline is a superset of another: the test suite
alone reaches 384, the replay alone 144, the batch run alone 1.

## The solution

First Light enumerates every function in a package, runs baselines under
instrumentation, and gives each one a provenance it refuses to blur:

- `never_observed`. No baseline reached it, and no driver has a verified claim.
- `observed_in_situ`. A baseline executed it during real operation.
- `observed_under_driver`. It ran only because an agent wrote code to reach it.

The third level carries the idea. Evidence an agent manufactured is weaker than
evidence from real use, and First Light records it as weaker. Every driver must
declare where the function is reached in production, and the tool parses that
file to confirm the name is called there. A driver that declares nothing, or
names a line that is not a call, is refused, and the evidence keeps both facts:
the driver reached the function, and its claim could not be checked.

The evidence file is machine-readable, and a Bob `PreToolUse` hook reads it
before every edit. Edit, Write and MultiEdit send no line number, so the hook
places the edit from the text they do send: `old_string`, present verbatim in
the file, or a diff of the new content. It then names the function and its
provenance. An edit crossing several functions reports all of them, least
observed first, and an ambiguous anchor is refused rather than guessed. Built
with IBM Bob across twelve tasks; `bob_sessions/` holds the record.

## Who uses it

Teams pointing coding agents at code nobody has verified. Adopting it means
writing one small runner script per baseline, which is the script that starts
your application the way it really runs; the three used here are in `runners/`.
After that, one pass produces the report and the evidence file, and the hook is
wired once.

## Does it change anything

`tools/ab_gate.py` runs the shipped hook over 79 real edit payloads built from
the evidence. Without the hook none is questioned, including the 37 that rewrite
code with no execution record. With it all 79 are placed and named from the
edited text alone, and `--strict` stops exactly those 37, with no observed
function stopped by mistake. Scripted edits, not a live agent: the claim is that
the control discriminates, not that a model changed its mind.

## Why this is new

Coverage tools count lines and test generators write tests. What neither does
is separate evidence gathered by watching a program work from evidence
manufactured to fill a gap, or publish that distinction for an agent to read.

Driver generation runs on watsonx.ai. `ibm/granite-4-h-small` drafts a driver
for a function nothing has been seen to execute, and the same gate judges it: of
the first five, two were accepted and three refused. Those two ship as
candidates, not as figures, because no human has reviewed them yet.

The tool demonstrated this on itself. The test suite made five of our agent's
ten drivers unnecessary and the replay made a sixth. Two more were refused
outright. Two promotions we had published did not survive later scrutiny, and
we published that too. It reports the work its own agent wasted and refuses its
own agent's unverifiable claims.
