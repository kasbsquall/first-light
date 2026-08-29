# First Light: problem and solution statement

## The problem

Agents now write code faster than anyone can read it, and they treat every
function the same. To an editing agent, a function exercised by a passing test
and a function that has never executed in its life look identical. Coverage does
not close that gap. Coverage reports that a line was touched. It says nothing
about whether anyone has ever observed what that function does when it runs.
Teams read a coverage percentage as confidence and let agents edit on that basis.

We measured visidata, a widely used Python tool. Of 2290 functions in its product
code, 1343 have never been observed executing by anything. That figure holds
after running the program through its own interface and after running the
project's own 256-test suite. Those two are not substitutes for each other: the
test suite reaches 682 functions the running program never touches, and the
running program reaches 10 the tests never touch.

## The solution

First Light enumerates every function in a package, runs baselines under
instrumentation, and assigns each function a provenance it refuses to blur:

- `never_observed`. Nothing has ever run this.
- `observed_in_situ`. A baseline executed it during real operation.
- `observed_under_driver`. It ran only because an agent wrote code to reach it.

The third level carries the idea. When an agent writes a driver to exercise dead
code, the evidence produced is weaker than evidence from real use, and First
Light records it as weaker instead of promoting it. Every driver must also
declare, in a comment, where the function is reached in production. The tool
opens that file and checks the claim. A driver that declares nothing, or names a
call site that cannot be confirmed, is refused.

The result is published as a machine-readable evidence file that a Bob
`PreToolUse` hook reads before every edit, so the agent is told what is known
about the function at the moment it changes it.

## Who uses it

Teams pointing coding agents at code nobody has verified. They run one command,
get a report and an evidence file, and wire the hook once. After that the
evidence travels with the agent.

## Why this is new

Test generators write tests. Coverage tools count lines. Neither publishes an
epistemic state that an agent consults while editing, and neither separates
evidence gathered by watching a program work from evidence manufactured to fill
a gap.

The tool demonstrated this on itself. Adding the test-suite baseline made five of
our own agent's ten drivers unnecessary, and we published that rather than
quietly deleting them. The call-site check then rejected two of our agent's
drivers. Closing the last gap in that check cost us a promotion we had already
counted, and we kept the closure and lowered the number.

A system that reports the work its own agent wasted, and refuses its own agent's
unverifiable claims, is the only kind worth trusting with the answer.
