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

cli_only = sum(1 for u in prod.values() if in_bl(u, "cli") and not in_bl(u, "test_suite"))
ts_only  = sum(1 for u in prod.values() if in_bl(u, "test_suite") and not in_bl(u, "cli"))
both     = sum(1 for u in prod.values() if in_bl(u, "cli") and in_bl(u, "test_suite"))
cli_total = cli_only + both
ts_total  = ts_only + both

print()
print("PRODUCT SCOPE (excl. tests/vendor/apps/experimental)")
print(f"  total                  = {total}")
print(f"  never_observed         = {never}")
print(f"  observed_in_situ       = {insitu}")
print(f"  driver_redundant       = {redundant}")
print(f"  observed_under_driver  = {driver}")
print(f"  observed (total)       = {obs}")
print()
print("PER-BASELINE BREAKDOWN (product scope)")
print(f"  cli total observed     = {cli_total}")
print(f"  test_suite total obs   = {ts_total}")
print(f"  cli alone (not in TS)  = {cli_only}")
print(f"  test_suite added over cli (unique) = {ts_only}")
print(f"  observed by both       = {both}")
