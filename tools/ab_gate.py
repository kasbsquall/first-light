#!/usr/bin/env python3
"""Does the hook change what happens to an edit? Measure it instead of arguing.

The rest of this project measures a codebase. This measures the tool: it takes
real functions out of the published evidence, builds a real Edit payload for
each one, and runs the real hook over it twice, once as an advisory and once
with --strict. The comparison is between the pipeline with the control and the
pipeline without it.

What it is honest about: these are scripted edits, not a live agent. Nobody is
claiming a model changed its mind. The claim is narrower and checkable, which is
the only kind this project makes: given N edits that a coding agent could
plausibly make, the control fires on N and stops Y, and without it all N proceed
with nothing said.

    python tools/ab_gate.py --evidence evidence.json --out ab-run.json

Standard library only, like everything else here. The hook runs as a subprocess
so this script cannot fabricate its answers: whatever the advisory says is
whatever the shipped hook actually printed.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "tools" / "fl_hook.py"

NEVER = "never_observed"
IN_SITU = "observed_in_situ"
DRIVER = "observed_under_driver"


def unique_anchor(text: str, body_start: int, body_end: int) -> str | None:
    """A line inside the body that appears exactly once in the file.

    Anything else would be an ambiguous anchor, which the hook refuses by
    design, so it would measure the refusal path rather than the located one.
    """
    lines = text.splitlines()
    for i in range(body_start - 1, min(body_end, len(lines))):
        candidate = lines[i]
        if len(candidate.strip()) < 12:
            continue
        if text.count(candidate) == 1:
            return candidate
    return None


def run_hook(file_path: str, anchor: str, strict: bool) -> tuple[str, int]:
    payload = json.dumps({"tool_input": {
        "file_path": file_path,
        "old_string": anchor,
        "new_string": anchor + "  # edited",
    }})
    cmd = [sys.executable, str(HOOK)] + (["--strict"] if strict else [])
    r = subprocess.run(cmd, input=payload, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--evidence", default=str(ROOT / "evidence.json"))
    ap.add_argument("--out", default=str(ROOT / "ab-run.json"))
    ap.add_argument("--per-tier", type=int, default=40)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    ev = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    units = ev["units"]

    # Sample from each tier so the run is not dominated by whichever is largest.
    # The seed is fixed and printed, so the sample is the same on a re-run.
    by_tier: dict[str, list] = {NEVER: [], IN_SITU: [], DRIVER: []}
    for key, u in units.items():
        if u.get("provenance") in by_tier and Path(u["file"]).is_file():
            by_tier[u["provenance"]].append((key, u))
    rng = random.Random(args.seed)
    chosen = []
    for tier, pool in by_tier.items():
        rng.shuffle(pool)
        chosen.extend(pool[: args.per_tier])

    cache: dict[str, str] = {}
    rows, skipped = [], 0
    for key, u in chosen:
        path = u["file"]
        if path not in cache:
            cache[path] = Path(path).read_text(encoding="utf-8", errors="replace")
        anchor = unique_anchor(cache[path], u["body_start"], u["body_end"])
        if anchor is None:
            skipped += 1
            continue
        advisory, _ = run_hook(path, anchor, strict=False)
        _, code = run_hook(path, anchor, strict=True)
        qname = key.split("::", 1)[1].rsplit("#", 1)[0] if "::" in key else key
        rows.append({
            "unit": qname,
            "provenance": u["provenance"],
            "located": "located from the edited text" in advisory,
            "named": qname in advisory,
            "blocked_under_strict": code == 2,
        })

    n = len(rows)
    located = sum(1 for r in rows if r["located"])
    named = sum(1 for r in rows if r["named"])
    blocked = sum(1 for r in rows if r["blocked_under_strict"])
    never_rows = [r for r in rows if r["provenance"] == NEVER]
    blocked_never = sum(1 for r in never_rows if r["blocked_under_strict"])
    wrongly_blocked = sum(
        1 for r in rows if r["provenance"] != NEVER and r["blocked_under_strict"]
    )

    out = {
        "_comment": (
            "Scripted edits, not a live agent. Each row is a real Edit payload "
            "carrying no line number, run against the shipped hook as a "
            "subprocess. Reproduce with tools/ab_gate.py --seed %d." % args.seed
        ),
        "seed": args.seed,
        "sampled": len(chosen),
        "skipped_no_unique_anchor": skipped,
        "edits": n,
        "without_hook": {
            "advisories": 0,
            "stopped": 0,
            "note": "No hook means no output and no gate: every edit proceeds "
                    "and nothing is said about any of them.",
        },
        "with_hook": {
            "located": located,
            "named_the_function": named,
            "stopped_under_strict": blocked,
            "stopped_that_were_never_observed": blocked_never,
            "stopped_that_were_observed": wrongly_blocked,
        },
        "rows": rows,
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("edits measured            : %d (%d sampled, %d had no unique anchor)"
          % (n, len(chosen), skipped))
    print("without the hook          : 0 advisories, 0 stopped")
    print("with the hook, located    : %d of %d" % (located, n))
    print("named the right function  : %d of %d" % (named, n))
    print("stopped under --strict    : %d" % blocked)
    print("  of those, never observed: %d" % blocked_never)
    print("  of those, observed      : %d  <- must be 0" % wrongly_blocked)
    print("written to                : %s" % args.out)
    return 0 if wrongly_blocked == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
