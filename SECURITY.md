# Security

## What this tool executes

First Light runs code in order to observe it. That is the point of the tool and
it is also its main risk. Three things get executed:

1. **The target package**, through a runner script you supply, under
   `coverage.py` in the target's own virtual environment.
2. **The target's test suite**, when you configure it as a second baseline.
3. **Drivers**, small scripts that call functions the baselines never reached.
   The drivers in this repository were written by an AI agent.

Point 3 is the one to be deliberate about. A driver is arbitrary Python that
runs with your privileges. Read a driver before promoting it, the same way you
would read any code an agent proposes to run. `--promote-driver` executes the
driver file you name.

## The measured process is patched

Before running a baseline, First Light injects a wrapper that replaces
`os._exit` with a call that raises `SystemExit`. Without it, a target that ends
by calling `os._exit` terminates the interpreter before `coverage.py` can write
its data, and the run silently produces nothing. The patch is scoped to the
runner subprocess and does not touch your interpreter, but it does mean the
measured process is not byte-for-byte the process you would run by hand.

## What the verification gate does and does not do

The gate checks that a driver genuinely reached the function it claims to reach,
and that the call site it declares can be opened and confirmed. It rejects a
driver that copies a function body instead of importing it, one that exits
non-zero, one whose declared call site does not exist, does not contain the
function's name, sits outside the package under analysis, or is a definition, an
import or a comment rather than a use. It rejects a driver that declares no call
site at all.

The gate is a correctness check on evidence, not a sandbox. It runs the driver
in order to measure it. It does not protect you from a malicious driver and it
is not designed to.

## Running against untrusted code

Do not point First Light at a package you would not be willing to import and
execute. Enumerating functions is static and safe. Establishing observation is
not, because observation requires running the program.

## Credentials

First Light needs no credentials, reads no secrets from the environment, and
makes no network calls. If you add an integration that does, keep the secret in
an environment variable on the machine and out of the repository. Nothing in
this codebase should ever contain a key.
