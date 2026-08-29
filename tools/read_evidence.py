import json, pathlib

ev = json.loads(pathlib.Path("evidence.json").read_text())

bls = ev["baselines"]
print("Baselines:")
for b in bls:
    bid = b["id"]
    rc  = b["exit_code"]
    rn  = pathlib.Path(b["runner"]).name
    print(f"  id={bid}  exit_code={rc}  runner={rn}")

units = ev["units"]
excl_dirs = set(bls[0].get("excluded_dirs", []))

def is_product(u):
    parts = set(pathlib.Path(u["file"]).parts)
    return not (parts & excl_dirs)

prod = {k: u for k, u in units.items() if is_product(u)}

total  = len(prod)
never  = sum(1 for u in prod.values() if u["provenance"] == "never_observed")
# The 5 units whose driver a baseline made redundant are observed_in_situ and
# are also reported on their own, so the two figures are named separately
# rather than folded together under one label.
redundant = sum(1 for u in prod.values() if u.get("driver_redundant_baseline"))
insitu = sum(1 for u in prod.values()
             if u["provenance"] == "observed_in_situ"
             and not u.get("driver_redundant_baseline"))
driver = sum(1 for u in prod.values() if u["provenance"] == "observed_under_driver")
obs    = insitu + driver

def in_bl(u, name):
    return name in u.get("observed_in_baseline", [])

# Report the split for whatever baselines the evidence actually carries. This
# was hardcoded to two, so it kept printing a cli-only figure from before the
# replay baseline existed.
from collections import Counter
combos = Counter()
for u in prod.values():
    bl = u.get("observed_in_baseline") or []
    if bl:
        combos[" + ".join(sorted(bl))] += 1
# These were computed and never printed, in the tool the README offers as the
# way to check the published figures. Print them.
print()
print("Product scope:")
print(f"  total                  = {total}")
print(f"  observed_in_situ       = {insitu}")
print(f"  observed_under_driver  = {driver}")
print(f"  driver_redundant       = {redundant}")
print(f"  never_observed         = {never}")
print(f"  sum                    = {insitu + driver + redundant + never}")
print()
print("  reached by")
for combo, n in sorted(combos.items(), key=lambda kv: -kv[1]):
    print(f"    {combo:<32} {n}")
for b in ev.get("baselines", []):
    bid = b.get("id")
    bl_total = sum(n for combo, n in combos.items() if bid in combo.split(" + "))
    alone = combos.get(bid, 0)
    print(f"  {bid:<12} observed {bl_total:>5}   only this one {alone:>4}")
