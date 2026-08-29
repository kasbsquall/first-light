# Bob session export

`bob-tasks-first-light-2026-08-29.json` is the full export of every Bob task used
to build First Light, produced with **Export Task History** from the Bob command
palette. It covers 10 tasks and 952 messages, in the order they were run.

Each entry below is one Bob task. The JSON holds the complete transcript for each:
every prompt, every tool call, every diff Bob proposed and whether it was accepted.

| # | Started (UTC) | Messages | Task |
|--:|---------------|---------:|------|
| 1 | Aug 28 22:20 | 161 | Build the first component of a tool called First Light |
| 2 | Aug 28 23:06 | 155 | Next component |
| 3 | Aug 29 02:57 | 66 | Build a visual report |
| 4 | Aug 29 03:36 | 76 | Three correctness bugs found in an independent audit |
| 5 | Aug 29 03:43 | 49 | Close the driver loop |
| 6 | Aug 29 03:49 | 117 | Repository hygiene, plus one architectural fix carried over from the previous task |
| 7 | Aug 29 04:17 | 92 | An independent audit found seven issues |
| 8 | Aug 29 05:20 | 93 | The evidence currently rests on a single baseline: one CLI session |
| 9 | Aug 29 05:36 | 77 | Regenerating the baselines silently destroyed every observed_under_driver record |
| 10 | Aug 29 06:12 | 66 | The superseded level works, but the report and the summary line hide it or blur it |

## How to read this

Tasks 1 to 3 build the tool: the observer and evidence model, driver generation
for functions that had never been observed, and the HTML evidence report.

Tasks 4 to 7 are corrections. Each one begins with findings from an independent
audit of the code Bob had just written, and Bob fixes them. Three of those tasks
exist because the audit caught the tool asserting something it had not verified,
which is the failure the tool itself was built to detect.

Task 8 adds the project's own test suite as a second, independent baseline. That
is what moved the headline claim from "we ran the program once" to a figure that
survives the obvious objection.

Tasks 9 and 10 close the remaining gaps the audits found in how evidence levels
were recorded and presented.

## What this export shows about the work

The record is not a clean line. Bob's first attempt at drivers copied function
bodies instead of importing the real functions, which marked functions as
observed when nothing had executed. That is visible in the transcripts, along
with the verification gate built in response. Three times Bob proposed a
correction to the plan that was better than the one it was given, and those are
visible too.
