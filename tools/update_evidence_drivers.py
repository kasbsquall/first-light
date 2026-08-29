#!/usr/bin/env python3
"""
update_evidence_drivers.py  —  mark observed_under_driver units in evidence.json.

For each (key, driver_path, call_site) tuple in DRIVER_RESULTS, find the
matching unit in evidence.json and update its provenance to
"observed_under_driver".  Units already marked "observed_in_situ" are never
downgraded — those are a different class of evidence.

Run from the workspace root:
    python tools/update_evidence_drivers.py
"""

import json
import os
from pathlib import Path

EVIDENCE_PATH = Path("target/visidata/evidence.json")

# ---------------------------------------------------------------------------
# Results table
# Each entry:  (evidence_key_suffix, driver_file, originating_call_site)
# The key suffix is the part after "::" in evidence.json keys.
# ---------------------------------------------------------------------------

DRIVER_RESULTS = [
    (
        "visidata.utils.moveListItem",
        "drivers/visidata.utils.moveListItem.py",
        "target/visidata/visidata/features/slide.py:22 -- moveListItem(sheet.rows, rowidx, newcolidx)",
    ),
    (
        "visidata.utils.getattrdeep",
        "drivers/visidata.utils.getattrdeep.py",
        "target/visidata/visidata/column.py:546 -- getattrdeep(row, col.expr, None)",
    ),
    (
        "visidata.utils.setattrdeep",
        "drivers/visidata.utils.setattrdeep.py",
        "target/visidata/visidata/column.py:551 -- setattrdeep(row, self.expr, val)",
    ),
    (
        "visidata.utils.colname_letters",
        "drivers/visidata.utils.colname_letters.py",
        "target/visidata/visidata/sheets.py:1086 -- colname_letters(self.colname_ctr)",
    ),
    (
        "visidata.aggregators.mean",
        "drivers/visidata.aggregators.mean.py",
        "target/visidata/visidata/features/ping.py:33 -- mean(r.getValues(col.sheet.source.rows))",
    ),
    (
        "visidata.aggregators.stdev",
        "drivers/visidata.aggregators.stdev.py",
        "target/visidata/visidata/aggregators.py:162 -- funcValues(vals) where funcValues=stdev",
    ),
    (
        "visidata.aggregators._percentile",
        "drivers/visidata.aggregators._percentile.py",
        "target/visidata/visidata/aggregators.py:198 -- _percentile(sorted(col.getValues(rows)), self.pct/100, key=float)",
    ),
    (
        "visidata.path.modtime",
        "drivers/visidata.path.modtime.py",
        "target/visidata/visidata/_urlcache.py:18 -- modtime(p)  # p is Path-like with .stat()",
    ),
    (
        "visidata.path.vstat",
        "drivers/visidata.path.vstat.py",
        "target/visidata/visidata/path.py:67 -- vstat(path)  # 0 direct call sites; defined in module, usage migrated to path.stat()",
    ),
    (
        "visidata.modify.changestr",
        "drivers/visidata.modify.changestr.py",
        "target/visidata/visidata/modify.py:345 -- sheet.changestr(adds, mods, deletes)",
    ),
]


def main() -> None:
    with open(EVIDENCE_PATH, encoding="utf-8") as fh:
        doc = json.load(fh)

    units: dict = doc["units"]

    updated = 0
    skipped_in_situ = 0
    not_found = 0

    for qname_suffix, driver_path, call_site in DRIVER_RESULTS:
        # Find matching key: the unit key ends with "::<qname_suffix>"
        matched_key = None
        for key in units:
            if key.endswith(f"::{qname_suffix}"):
                matched_key = key
                break

        if matched_key is None:
            print(f"  NOT FOUND  {qname_suffix}")
            not_found += 1
            continue

        unit = units[matched_key]
        current = unit["provenance"]

        if current == "observed_in_situ":
            # Never downgrade in-situ evidence — different class
            print(f"  SKIP (in_situ)  {qname_suffix}")
            skipped_in_situ += 1
            continue

        # Resolve driver path to absolute so it is unambiguous in the artifact
        abs_driver = str(Path(driver_path).resolve())

        unit["provenance"] = "observed_under_driver"
        unit["driver"] = abs_driver
        unit["call_site"] = call_site
        updated += 1
        print(f"  UPDATED  {qname_suffix}")

    with open(EVIDENCE_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)

    print()
    print(f"Summary: {updated} updated, {skipped_in_situ} skipped (in_situ), {not_found} not found")
    print(f"Evidence written to {EVIDENCE_PATH}")


if __name__ == "__main__":
    main()
