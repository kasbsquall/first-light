# Candidates, not evidence

Everything in this directory was drafted by `ibm/granite-4-h-small` on
watsonx.ai via `tools/wx_draft_driver.py`. Nothing here is folded into
`evidence.json` and no published figure depends on it.

The gate's verdict on each of these, and the reason they are kept separate, is
in [../docs/watsonx.md](../docs/watsonx.md).

To judge them yourself, against a copy of the evidence rather than the published
one:

```
python first_light.py --promote-driver --all --drivers-dir drivers-candidates --evidence <a copy>
```
