# First Light: problem and solution statement

## The problem

Agents write code faster than anyone can read it, and treat every function the
same. To an editing agent, a function exercised by a passing test and one that
has never executed look identical. Coverage does not close that gap: it reports
that a line was touched, not whether anyone watched the function run. Teams read
a coverage percentage as confidence and let agents edit on that.

We measured visidata, a widely used Python tool. Of its 2290 product functions,
1200 have never been observed executing. That holds after running the program in
batch mode, after its own 256-test suite, and after replaying all 52 session
logs the project ships. No baseline is a superset of another: the test suite
alone reaches 384, the replay alone 144, the batch run alone 1.

## The solution

First Light enumerates every function in a package, runs baselines under
instrumentation, and assigns each function a provenance it refuses to blur:

- `never_observed`. No baseline reached it, and no driver has a verified claim.
- `observed_in_situ`. A baseline executed it during real operation.
- `observed_under_driver`. It ran only because an agent wrote code to reach it.

The third level carries the idea. Evidence an agent manufactured is weaker than
evidence from real use, and First Light records it as weaker. Every driver must
declare where the function is reached in production, and the tool parses that
file to confirm the line really calls it. A driver that declares nothing, or
names a line that is not a call, is refused, and the evidence keeps both facts:
the driver reached the function, and its claim could not be checked.

The evidence file is machine-readable, and a Bob `PreToolUse` hook reads it
before every edit. With a line number it names that function's provenance;
without one it reports the file's tracked state and says the edit could not be
placed. Built with IBM Bob across twelve tasks; `bob_sessions/` holds the record.

## Who uses it

Teams pointing coding agents at code nobody has verified. One pass produces the report
and the evidence file; the hook is wired once.

## Why this is new

Coverage tools count lines and test generators write tests. What neither does
is separate evidence gathered by watching a program work from evidence
manufactured to fill a gap, or publish that distinction for an agent to read.

Nothing here runs on watsonx.ai. Driver generation is where it belongs and the
verifier is the hard half, which exists; the event account's catalog offered no
AI category, so the runtime could not be provisioned. We would rather say so
than ship an integration that does not run.

The tool demonstrated this on itself. The test suite made five of our agent's
ten drivers unnecessary and the replay made a sixth. Two more were refused
outright. Three promotions we had published did not survive later scrutiny, and
we published that too. It reports the work its own agent wasted and refuses its
own agent's unverifiable claims.
