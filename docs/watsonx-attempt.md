# The watsonx.ai attempt, and what it returned

The README says driver generation belongs on watsonx.ai and that we could not
build it. This file is the evidence for that, because a claim with no artifact
behind it is the thing this project exists to refuse, and an excuse is still a
claim.

Captured 2026-08-29 against the IBM Cloud account provisioned for the event.

---

## 1. Authentication succeeds

Exchanging the account's API key for an IAM token works. Nothing about the
credentials is the problem.

```
POST https://iam.cloud.ibm.com/identity/token
grant_type=urn:ibm:params:oauth:grant-type:apikey

200 OK
{"access_token": "...", "expires_in": 3600, "token_type": "Bearer"}
```

## 2. The account can see the models

```
GET https://us-south.ml.cloud.ibm.com/ml/v1/foundation_model_specs?version=2024-05-01

200 OK   20 models
```

The Granite entries it returns, with the functions each supports:

```
ibm/granite-3-1-8b-base                  base_foundation_model_deployable, lora_fine_tune_trainable
ibm/granite-4-h-small                    autoai_rag, text_chat, text_generation
ibm/granite-embedding-278m-multilingual  autoai_rag, embedding
ibm/granite-guardian-3-8b                text_chat, text_generation
ibm/granite-ttm-1024-96-r2               time_series_forecast
ibm/granite-ttm-1536-96-r2               time_series_forecast
ibm/granite-ttm-512-96-r2                time_series_forecast
```

`ibm/granite-4-h-small` is the one that would have generated drivers. It is
visible to the account and it supports text generation.

## 3. Inference is refused

```
POST https://us-south.ml.cloud.ibm.com/ml/v1/text/generation?version=2023-05-29
{"model_id": "ibm/granite-4-h-small", "input": "...", "project_id": "b641beb5-...", ...}

403 Forbidden
{
  "errors": [
    {
      "code": "no_associated_service_instance_error",
      "message": "project_id b641beb5-... is not associated with a WML instance",
      "more_info": "https://cloud.ibm.com/apidocs/watsonx-ai#text-generation"
    }
  ],
  "status_code": 403
}
```

## 4. The runtime cannot be provisioned from this account

Inference requires a watsonx.ai Runtime instance associated with the project.
The project's own "associate service" dialog offers watsonx.ai Studio, watsonx
Assistant, watsonx.governance, watsonx.data integration and IBM Knowledge
Catalog. It does not offer the Runtime.

Creating one from the account's catalog also fails. The catalog lists twelve
products and has no AI or machine learning category at all:

```
Container Registry            Observability
Containers                    Platform Automation
Context-Based Restrictions    Security
Databases                     Transit Gateway
Direct Link                   VCF as a Service
IAM Access Management         Infrastructure
```

Searching it for `machine learning` and for `runtime` returns nothing.
Provisioning Watson Machine Learning through the project dialog returns
`An error occurred while retrieving data from global catalog`.

---

## What this means and what it does not

The limit is the service catalog exposed to this event account, not the
platform. An account with watsonx.ai Runtime available would reach the same
model the specs endpoint already lists here.

We did not build the integration and nothing in this repository calls
watsonx.ai. Publishing one that does not run would be the same unverified
assertion the tool exists to detect, and saying "we could not" without showing
what came back would be a second one.
