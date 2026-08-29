# First Light: problem and solution statement

## The problem

Agents write code faster than anyone can read it, and they treat every function
the same. To an editing agent, a function exercised by a passing test and one
that has never executed look identical. Coverage does not close that gap: it
reports that a line was touched, and says nothing about whether anyone has ever
observed what the function does when it runs. Teams read a coverage percentage
as confidence and let agents edit on that basis.

We measured visidata, a widely used Python tool. Of 2290 functions in its product
code, 1343 have never been observed executing by anything. That figure holds
after running the program through its own interface and after running the
project's own 256-test suite. Those two are not substitutes for each other: the
test suite reaches 682 functions the running program never touches, and the
running program reaches 10 the tests never touch.

## The solution

First Light enumerates every function in a package, runs baselines under
instrumentation, and assigns each function a provenance it refuses to blur:

- `never_observed`. No baseline reached it, and no driver has produced a
  verified claim about it.
- `observed_in_situ`. A baseline executed it during real operation.
- `observed_under_driver`. It ran only because an agent wrote code to reach it.

The third level carries the idea. Evidence an agent manufactured is weaker than
evidence from real use, and First Light records it as weaker instead of
promoting it. Every driver must also declare where the function is reached in
production. The tool opens that file and checks it. A driver that declares
nothing, or names a call site that cannot be confirmed, is refused, and the
evidence keeps both facts: the driver did reach the function, and its claim
could not be checked.

The evidence file is machine-readable, and a Bob `PreToolUse` hook reads it
before every edit, so the agent is told what is known about the function as it
changes it. First Light was built with IBM Bob across ten tasks; `bob_sessions/`
holds that record and an index of what it shows.

## Who uses it

Teams pointing coding agents at code nobody has verified. One command produces
the report and the evidence file; the hook is wired once. After that the
evidence travels with the agent.

## Why this is new

Coverage tools count lines and test generators write tests. What neither does
is separate evidence gathered by watching a program work from evidence
manufactured to fill a gap, or publish that distinction for an agent to read
while editing.

The tool demonstrated this on itself. Adding the test-suite baseline made five of
our own agent's ten drivers unnecessary, and we published that rather than
quietly deleting them. The call-site check then rejected two of our agent's
drivers. Closing the last gap in that check cost us a promotion we had already
counted, and we kept the closure and lowered the number.

It reports the work its own agent wasted and refuses its own agent's
unverifiable claims. That is the standard the answer has to meet.
