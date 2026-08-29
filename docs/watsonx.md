# watsonx.ai: what failed, why, and what it produced

This project refuses claims that nothing checked. That standard applies to our
own account of using watsonx.ai, so this file carries the whole arc, including
the part where the failure was ours.

Captured 2026-08-29 against the IBM Cloud account provisioned for the event.

---

## 1. The first attempt failed, and we blamed the wrong thing

Authentication worked from the start. Exchanging the account's API key for an
IAM token returns 200, and the account can list foundation models:

```
POST https://iam.cloud.ibm.com/identity/token
200 OK

GET https://us-south.ml.cloud.ibm.com/ml/v1/foundation_model_specs?version=2024-05-01
200 OK   20 models, 7 of which support text generation
```

Inference did not:

```
POST https://us-south.ml.cloud.ibm.com/ml/v1/text/generation?version=2023-05-29
403 Forbidden
{"code": "no_associated_service_instance_error",
 "message": "project_id b641beb5-... is not associated with a WML instance"}
```

Creating a Runtime from the catalog failed too, with
`An error occurred while retrieving data from global catalog`, and the catalog
listed twelve infrastructure products with no AI category. We concluded the
account could not reach watsonx.ai and wrote that down.

That conclusion was wrong, and the error message had already said so. It named
`project_id b641beb5-...` and reported that **that project** had no WML
instance. We were pointing at a project we had created ourselves.

## 2. The cause

The event provisions a cloud account that already contains a project with the
Runtime attached. Creating your own project, or your own service, is neither
necessary nor permitted: the permission error is expected behaviour, and the
account is regionally pinned to Dallas.

The preconfigured project is `watsonx Hackathon Sandbox`, and its
`Services and integrations` panel lists one entry, `watsonx Challenge WML`, of
type `watsonx.ai Runtime`. That association is the thing our own project lacked.

## 3. With the right project, it answers

```
POST https://us-south.ml.cloud.ibm.com/ml/v1/text/chat?version=2024-10-08
{"model_id": "ibm/granite-4-h-small", "project_id": "26175715-009c-4f1d-9b0a-adde2b9401aa", ...}

200 OK
{"model_id": "ibm/granite-4-h-small", "model_version": "4.0.0",
 "results": [{"generated_text": "ok", "stop_reason": "eos_token"}]}
```

Note the endpoint. `/ml/v1/text/generation` still works and returns a
deprecation warning pointing at `/ml/v1/text/chat`, so the tool uses chat.

## 4. What we built with it

`tools/wx_draft_driver.py` asks `ibm/granite-4-h-small` to draft a driver for a
function that has never been observed executing. It gathers the function's
source and every line in the package that calls it by name, and asks for a
script that imports the real function and declares a verifiable call site.

The tool promotes nothing. It writes into `drivers-candidates/` and stops. The
same gate that judges a hand-written driver then judges the model's, with the
same rules and the same refusal classes.

Credentials come from the environment and never from this repository:

```
WATSONX_APIKEY        the key, or
WATSONX_APIKEY_FILE   a path to a file holding it
WATSONX_PROJECT_ID    the project the key can reach
```

## 5. What the gate said about it

Five functions, chosen because each is genuinely called somewhere in the package
and never observed running. The model drafted five drivers:

```
FAIL  visidata.color.rgb_to_attr          driver process exited with non-zero return code
OK    visidata.graph.format_input_value   lines [371, 374]
FAIL  visidata.menu.menudraw              driver process exited with non-zero return code
FAIL  visidata.optionssheet.commit        driver process exited with non-zero return code
OK    visidata.threads.codestr            lines [500, 501]

2/5 promoted, 3 failed
```

The two accepted ones cite real lines. `graph.py:324` is
`suggested = format_input_value(val, xtype)` and `threads.py:436` is
`Column('funcname', getter=lambda col,row: codestr(row.code))`. Both drivers
import the real function rather than redefining it.

Three failed because the driver did not run, which is the honest majority
outcome and the reason a gate exists.

## 6. Why the headline figures did not move

The two accepted drivers are not folded into the published `evidence.json`. They
were produced hours before submission and no human has reviewed the fixtures
they build, and a number this project puts on a page has to be one we are
willing to defend line by line. Promoting them would move `never_observed` from
1200 to 1198 on the strength of an unreviewed model draft, which is the shape of
claim this whole project argues against.

So the candidates ship as candidates. The tool is real, the run is reproducible,
and the gate's verdict on the model's output is recorded above rather than
summarised in our favour.

To reproduce:

```
set WATSONX_APIKEY_FILE and WATSONX_PROJECT_ID in the environment
python tools/wx_draft_driver.py --unit graph.format_input_value
python first_light.py --promote-driver --all --drivers-dir drivers-candidates --evidence <a copy>
```

Run it against a copy of the evidence. Pointing it at the published file would
edit the artifact the report is built from.
