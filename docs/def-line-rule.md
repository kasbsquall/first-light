# Where the 94% and the 11% came from

The written submission carries one figure that nothing in this repository
regenerates:

> Measured against a single CLI baseline, that one rule moved the result from
> 94% of functions reported observed to 11%.

A reviewer called it the only number in the submission a judge cannot check.
That is correct, and rather than drop the sentence or leave the reader to trust
it, here is exactly what it was and why it does not come back.

## What was measured

The first working version of First Light marked a function observed when the
line carrying its `def` keyword appeared in the coverage line set. That line
executes at import time for every function in an imported module, whether or not
anything ever calls it, so the measurement reported almost the entire package as
observed. On the target at that moment it read **94%**.

The rule that replaced it starts the observation range at
`node.body[0].lineno`, the first statement inside the function. Re-running the
same baseline with that change gave **11%**.

Both numbers were produced on **one baseline, the batch CLI run**, before the
test suite and the replay existed. 11% is 262 of 2,290, and 262 is still the
`cli` figure published in the report today.

## Why it is not reproducible from what ships here

There is no `--count-def-lines` flag, and adding one now would mean re-running
the pipeline hours before a deadline against an evidence file whose figures are
already published. The safer thing is to say plainly that the pair is a
historical measurement of a configuration this tool no longer has.

## What the shipped artifacts do support

The comparable current figure is **47%**: 1,090 of 2,290 product functions
reached by any means across all three baselines. That is on the report, it
reconciles with `evidence.json`, and a reader can check it.

So: the effect of the `body[0].lineno` rule is real and it is the single largest
methodological decision in the tool. The specific 94-against-11 pair is the
measurement that led us to it, taken on a narrower baseline than the one we
publish. Quoting it without that scope was the mistake; the sentence now carries
it, and this file exists so the scope points somewhere.
