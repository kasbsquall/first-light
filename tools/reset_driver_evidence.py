#!/usr/bin/env python3
"""Reset the 10 falsely-marked observed_under_driver units back to never_observed."""

import json
from pathlib import Path

EVIDENCE_PATH = Path("target/visidata/evidence.json")

SUFFIXES = [
    "visidata.utils.moveListItem",
    "visidata.utils.getattrdeep",
    "visidata.utils.setattrdeep",
    "visidata.utils.colname_letters",
    "visidata.aggregators.mean",
    "visidata.aggregators.stdev",
    "visidata.aggregators._percentile",
    "visidata.path.modtime",
    "visidata.path.vstat",
    "visidata.modify.changestr",
]

with open(EVIDENCE_PATH, encoding="utf-8") as fh:
    doc = json.load(fh)

units = doc["units"]
reset = 0
for key, unit in units.items():
    qname = key.split("::")[-1]
    if qname in SUFFIXES and unit["provenance"] == "observed_under_driver":
        unit["provenance"] = "never_observed"
        unit.pop("driver", None)
        unit.pop("call_site", None)
        reset += 1
        print(f"  RESET  {qname}")

with open(EVIDENCE_PATH, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2)

print(f"\nReset {reset} units back to never_observed.")
