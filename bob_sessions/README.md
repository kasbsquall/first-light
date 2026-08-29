# Bob session export

This folder holds the full export of every Bob task used to build First Light,
produced with **Export Task History** from the Bob command palette. It covers
12 tasks and 1145 messages: 350 from Bob, 19 from the human, 764 tool results
and 12 system prompts. The message count is mostly machine traffic, which is
worth saying before the number reads as more conversation than it was.

- `bob-tasks-first-light-2026-08-29.md` is the readable transcript. Start here. It is 647KB.
- `bob-tasks-first-light-2026-08-29.json` is the same record in full fidelity, including tool results.
  It is 22MB and is meant for machines.

The table below lists the tasks in the order they were run. Both files store
them newest first, so both are the reverse of this table.

| # | Started (UTC) | Messages | Task |
|--:|---------------|---------:|------|
| 1 | Aug 28 22:20 | 161 | Build the first component of a tool called First Light |
| 2 | Aug 28 23:06 | 155 | Next component |
| 3 | Aug 29 02:57 | 66 | Build a visual report |
| 4 | Aug 29 03:36 | 76 | Three correctness bugs found in an independent audit |
| 5 | Aug 29 03:43 | 49 | Close the driver loop |
| 6 | Aug 29 03:49 | 117 | Repository hygiene, plus one architectural fix carried over from the previo... |
| 7 | Aug 29 04:17 | 92 | An independent audit found seven issues |
| 8 | Aug 29 05:20 | 93 | The evidence currently rests on a single baseline: one CLI session |
| 9 | Aug 29 05:36 | 77 | Regenerating the baselines silently destroyed every observed_under_driver r... |
| 10 | Aug 29 06:12 | 66 | The superseded level works, but the report and the summary line hide it or... |
| 11 | Aug 29 08:04 | 108 | The README argues that evidence.json is an evaluation harness for generated... |
| 12 | Aug 29 10:00 | 85 | The headline rests on two baselines, and one of them is carrying almost not... |

## How to read this

Tasks 1 to 3 build the tool: the observer and evidence model, driver generation
for functions that had never been observed, and the HTML evidence report.

Tasks 4 and 7 begin with findings from an independent audit of the code Bob had
just written. Task 5 cites an auditor mid-prompt rather than opening with one,
and task 6 is a self-review carry-over with no audit in it at all. Several of these exist because something in the project asserted what
its own evidence did not support, which is the failure the tool was built to
detect. The prompt that opens task 6 says so in as many words: it notes that
this was the second time an artifact in this project had asserted something
the evidence did not support.

Tasks 8 and 12 each add a baseline. Task 8 adds the project's own test suite.
Task 12 replays the 52 sessions the project ships as recorded command logs,
because the batch CLI run it had been relying on turned out to reach almost
nothing the other baselines did not.

Tasks 9, 10 and 11 close gaps in how evidence was recorded and presented. Task
11 is the one that turned an argument in the README into a working feature: the
refusal reasons were being computed, printed and thrown away, and they are now
recorded as a machine-readable class on the unit.

## Two tasks did not finish

Task 3 ended on `BudgetExceededError` and task 9 on `TrialExpiredError`. Both
ran out of Bob budget mid-work rather than completing. Task 9 is the worse of
the two: its last recorded line is Bob part-way through a correction, and it
never delivers a result. The work both were doing was finished afterwards, and
the git history shows where.

They appear in the table as ordinary rows because that is what the export
records. Saying so here is cheaper than letting a reader open the JSON, find
`"status": "error"` on two of twelve, and wonder what else the index smoothed
over.

## What this export does not cover

Commits made between and after the Bob tasks are not in it, and they were not Bob tasks:
they are direct edits made in response to independent audits of the code Bob had
written. They include closing three separate bypasses in the promotion gate, separating the
record of a driver reaching a function from the record of it justifying its
claim, and the design work on the report.

The git history and this export therefore do not line up one to one, and saying
so is cheaper than letting a reader discover it. Bob built the tool. The work
between and after the tasks was correction, and most of it exists because an
audit found this project asserting something its own evidence did not support.

## What this export shows about the work

The record is not a clean line. Bob's first attempt at drivers copied function
bodies inline instead of importing the real functions, which marked ten
functions as observed on the strength of drivers that never called the real
code. A human caught it. One of the ten did produce genuine coverage, by
exec'ing the source file directly, and Bob found that itself while building
the gate. Bob then built the coverage gate that rejects that approach and proved the gate works by
writing a deliberately bad inline-copy driver and confirming the real source
file showed zero hits.

At least once, Bob declined an instruction and proposed something better. Told
to patch `os._exit` inside `first_light.py`, Bob pointed out that the patch
belongs in the runner subprocess, because that is the process that calls it,
and proposed generating a wrapper script instead. The instruction as given
would not have worked. This one is quoted because it is the one that survives a
search of the transcript; a higher count would be a claim this record does not
clearly support.

Adding baselines cost the project its own numbers twice, and both times that is
recorded rather than smoothed over. The test suite made five of the agent's ten
drivers redundant. The session replay made a sixth redundant and reduced
confirmed promotions from three to two.

## A limitation of the Markdown

The Markdown transcript records 764 of the 771 tool calls with their arguments,
and none of the tool results. The seven it omits are aborted calls that carry no
arguments, so nothing is lost there; the results are a different matter. Bob's narration says a run worked; the output that
would prove it is in the JSON. For a project whose thesis is that a claim should
travel with its evidence, that is worth stating plainly rather than leaving a
reader to find it.
