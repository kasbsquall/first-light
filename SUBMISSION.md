# First Light: problem and solution statement

## The problem

Agents write code faster than anyone can read it, and treat every function the
same. To an editing agent, a function exercised by a passing test and one that
has never executed look identical. Coverage does not close that gap: it reports
that a line was touched, not whether anyone has watched the function run. Teams
read a coverage percentage as confidence and let agents edit on that.

We measured visidata, a widely used Python tool. Of 2290 functions in its
product code, 1200 have never been observed executing. That figure holds after
running the program in batch mode, after its own 256-test suite, and after
replaying all 52 session logs the project ships. No baseline is a superset of
any other: the test suite alone reaches 384 product functions; the replay alone
reaches 144; the batch run alone reaches 1. After all three, 1200 product
functions have no execution record at all.

## The solution

First Light enumerates every function in a package, runs baselines under
instrumentation, and assigns each function a provenance it refuses to blur:

- `never_observed`. No baseline reached it, and no driver has a verified claim.
- `observed_in_situ`. A baseline executed it during real operation.
- `observed_under_driver`. It ran only because an agent wrote code to reach it.

The third level carries the idea. Evidence an agent manufactured is weaker than
evidence from real use, and First Light records it as weaker. Every driver must
declare where the function is reached in production. The tool opens that file
and checks it. A driver that declares nothing, or names a call site that cannot
be confirmed, is refused, and the evidence keeps both facts: the driver reached
the function, and its claim could not be checked.

The evidence file is machine-readable, and a Bob `PreToolUse` hook reads it
before every edit. When the payload carries a line number the hook names that
function's provenance; when it does not, it reports the file's tracked state and
says the edit could not be placed. First Light was built with IBM Bob across
twelve tasks; `bob_sessions/` holds that record.

## Who uses it

Teams pointing coding agents at code nobody has verified. One command produces
the report and evidence file; the hook is wired once. Evidence travels with the
agent.

## Why this is new

Coverage tools count lines and test generators write tests. What neither does
is separate evidence gathered by watching a program work from evidence
manufactured to fill a gap, or publish that distinction for an agent to read.

The tool demonstrated this on itself. The test-suite baseline made five of our
agent's ten drivers unnecessary. The session-replay baseline moved 144 more
functions to observed and made one more driver redundant. Both changes lowered
the count, and we published them. The call-site check then rejected two drivers;
closing that cost a promotion we had counted, and we took the loss. It reports
the work its own agent wasted and refuses its own agent's unverifiable claims.
