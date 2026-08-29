#!/usr/bin/env python3
"""
update_evidence_drivers_v2.py
Mark observed_under_driver in evidence.json ONLY for functions whose body
lines were confirmed executed in the real source file by coverage.py.

Each entry records:
  - provenance: "observed_under_driver"
  - driver: absolute path to the driver script
  - call_site: originating call site file:line from AST scan
  - coverage_confirmed_lines: the exact lines hit in the real source file
    (from the coverage CONFIRMED output above — this is what makes the claim defensible)

Run from workspace root:
    python tools/update_evidence_drivers_v2.py
"""

import json
from pathlib import Path

EVIDENCE_PATH = Path("target/visidata/evidence.json")

# Each entry: (qname_suffix, driver_rel_path, call_site, coverage_confirmed_lines)
# coverage_confirmed_lines = the exact lines reported by run_driver_with_coverage.py
CONFIRMED_RESULTS = [
    (
        "visidata.utils.moveListItem",
        "drivers/visidata.utils.moveListItem.py",
        "target/visidata/visidata/features/slide.py:22 -- moveListItem(sheet.rows, rowidx, newcolidx)",
        [65, 66, 67, 68, 69],
    ),
    (
        "visidata.utils.getattrdeep",
        "drivers/visidata.utils.getattrdeep.py",
        "target/visidata/visidata/column.py:546 -- getattrdeep(row, col.expr, None)",
        [87, 88, 89, 92, 93, 95, 97, 98, 100, 101, 102, 104, 105, 106, 107],
    ),
    (
        "visidata.utils.setattrdeep",
        "drivers/visidata.utils.setattrdeep.py",
        "target/visidata/visidata/column.py:551 -- setattrdeep(row, self.expr, val)",
        [112, 113, 115, 116, 117, 118, 119, 121, 122, 128],
    ),
    (
        "visidata.utils.colname_letters",
        "drivers/visidata.utils.colname_letters.py",
        "target/visidata/visidata/sheets.py:1086 -- colname_letters(self.colname_ctr)",
        [222, 223, 224, 225, 226, 227, 228, 229, 230, 231],
    ),
    (
        "visidata.aggregators.mean",
        "drivers/visidata.aggregators.mean.py",
        "target/visidata/visidata/features/ping.py:33 -- mean(r.getValues(col.sheet.source.rows))",
        [148, 149, 150],
    ),
    (
        "visidata.aggregators.stdev",
        "drivers/visidata.aggregators.stdev.py",
        "target/visidata/visidata/aggregators.py:162 -- funcValues(vals) where funcValues=stdev",
        [161, 162],
    ),
    (
        "visidata.aggregators._percentile",
        "drivers/visidata.aggregators._percentile.py",
        "target/visidata/visidata/aggregators.py:198 -- _percentile(sorted(col.getValues(rows)), self.pct/100, key=float)",
        [179, 180, 181, 182, 183, 184, 185, 186, 187, 188],
    ),
    (
        "visidata.path.modtime",
        "drivers/visidata.path.modtime.py",
        "target/visidata/visidata/_urlcache.py:18 -- modtime(p) where p is a visidata Path",
        [85, 86],
    ),
    (
        "visidata.path.vstat",
        "drivers/visidata.path.vstat.py",
        "target/visidata/visidata/path.py:67 -- vstat(path_string); 0 direct call sites in AST scan",
        [69, 70, 71, 72],
    ),
    (
        "visidata.modify.changestr",
        "drivers/visidata.modify.changestr.py",
        "target/visidata/visidata/modify.py:345 -- sheet.changestr(adds, mods, deletes)",
        [324, 325, 326, 328, 329, 330, 332, 333, 334, 336],
    ),
]


def main() -> None:
    with open(EVIDENCE_PATH, encoding="utf-8") as fh:
        doc = json.load(fh)

    units: dict = doc["units"]
    updated = 0
    skipped_in_situ = 0
    not_found = 0

    for qname_suffix, driver_path, call_site, confirmed_lines in CONFIRMED_RESULTS:
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
        if unit["provenance"] == "observed_in_situ":
            print(f"  SKIP (in_situ)  {qname_suffix}")
            skipped_in_situ += 1
            continue

        abs_driver = str(Path(driver_path).resolve())

        unit["provenance"] = "observed_under_driver"
        unit["driver"] = abs_driver
        unit["call_site"] = call_site
        unit["coverage_confirmed_lines"] = confirmed_lines
        updated += 1
        print(f"  UPDATED  {qname_suffix}  lines={confirmed_lines[:4]}{'...' if len(confirmed_lines)>4 else ''}")

    with open(EVIDENCE_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)

    print(f"\nSummary: {updated} updated, {skipped_in_situ} skipped (in_situ), {not_found} not found")
    print(f"Evidence written to {EVIDENCE_PATH}")


if __name__ == "__main__":
    main()
