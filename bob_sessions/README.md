# Bob session export

This folder holds the full export of every Bob task used to build First Light,
produced with **Export Task History** from the Bob command palette. It covers
10 tasks and 952 messages.

- `bob-tasks-first-light-2026-08-29.md` is the readable transcript. Start here.
  Note that it runs **newest task first**, the reverse of the table below.
- `bob-tasks-first-light-2026-08-29.json` is the same record in full fidelity,
  including tool results. It is 19MB and is meant for machines.

The table lists the tasks in the order they were run, which is the order they
appear in the JSON and the reverse of the order they appear in the Markdown.

| # | Started (UTC) | Messages | Task |
|--:|---------------|---------:|------|
| 1 | Aug 28 22:20 | 161 | Build the first component of a tool called First Light |
| 2 | Aug 28 23:06 | 155 | Next component |
| 3 | Aug 29 02:57 | 66 | Build a visual report |
| 4 | Aug 29 03:36 | 76 | Three correctness bugs found in an independent audit |
| 5 | Aug 29 03:43 | 49 | Close the driver loop |
| 6 | Aug 29 03:49 | 117 | Repository hygiene, plus one architectural fix carried over |
| 7 | Aug 29 04:17 | 92 | An independent audit found seven issues |
| 8 | Aug 29 05:20 | 93 | The evidence rests on a single baseline: one CLI session |
| 9 | Aug 29 05:36 | 77 | Regenerating the baselines destroyed every driver record |
| 10 | Aug 29 06:12 | 66 | The report hides or blurs the superseded level |

## How to read this

Tasks 1 to 3 build the tool: the observer and evidence model, driver generation
for functions that had never been observed, and the HTML evidence report.

Tasks 4, 5 and 7 begin with findings from an independent audit of the code Bob
had just written. Task 6 is a self-review carry-over rather than an audit
hand-off. Three of these tasks exist because something in the project asserted
what the evidence did not support, which is the failure the tool was built to
detect. Task 6 states it directly: "This is the second time an artifact in this
project asserted something the evidence did not support."

Task 8 adds the project's own test suite as a second, independent baseline. That
is what moved the headline claim from "we ran the program once" to a figure that
survives the obvious objection.

Tasks 9 and 10 close the remaining gaps the audits found in how evidence levels
were recorded and presented.

## What this export shows about the work

The record is not a clean line. Bob's first attempt at drivers copied function
bodies inline instead of importing the real functions, which marked ten
functions as observed when nothing had executed. A human caught it. Bob then
built the coverage gate that rejects that approach and proved the gate works by
writing a deliberately bad inline-copy driver and confirming the real source
file showed zero hits.

Twice, Bob declined an instruction and proposed something better. The clearest
case: told to patch `os._exit` inside `first_light.py`, Bob pointed out the
patch belongs in the runner subprocess, because that is the process that calls
it, and proposed generating a wrapper script instead. The instruction as given
would not have worked.

## A limitation of the Markdown

The Markdown transcript records every tool call and its arguments, but not the
tool results. Bob's narration says a run worked; the output that would prove it
is only in the JSON. For a project whose thesis is that a claim should travel
with its evidence, that is worth stating plainly rather than leaving a reader to
discover it.
