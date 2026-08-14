# Report Structure

## The human-readable report

Build the `.docx` (or Markdown, if that's what the user asked for) with
these sections, in this order. Populate every field with real values from
the recording — no placeholders.

```markdown
# BlazeMeter Recording Analysis — {recording name / project name}

## Overview & Source
- Source file: {filename}, format: {JMX | HAR | BlazeMeter YAML}
- Platform detected: {platform_guess}
- Auth model detected: {auth_model_guess}
- Business-relevant requests: {total_requests_parsed} ({noise_filtered_count} noise entries filtered out — static assets, analytics, CDN)
- Scenarios / thread groups: {count and names}

## Business Flow
{One paragraph describing the overall journey in plain language — e.g.
"This recording captures a single linear guest-checkout journey: browse,
search, add to cart, and complete payment. There is no login step and no
logout/session-end step."}

| # | Business Step | Method + Path | Hit Count / Weight | Think Time | Notes |
|---|---|---|---|---|---|
| 1 | Login | POST /customer/account/loginPost/ | 1 | 1.0s | |
| 2 | Search | GET /catalogsearch/result/ | 1 | 2.0s | |
| ... | | | | | |

## Data Parametrization
Values that should come from test data (a CSV row per virtual user), not be
replayed verbatim.

| Field | Observed In | Sample Value | Recommended Column | Notes |
|---|---|---|---|---|
| username | Login | testuser1@example.com | username | only 1 sample observed — add variety |
| sku | Add to Cart | SKU12345 | sku | |

## Correlation & Dynamic Values
Values the server generates mid-session that must be extracted from a
prior response and replayed — hardcoding these breaks the test after the
first run.

| Field | First Produced By | Consumed By | Extraction Strategy | Notes |
|---|---|---|---|---|
| form_key | Any page load | Login, Add to Cart, Checkout | Regex on response HTML: `form_key.*?value="(.+?)"` | |
| quoteId | Add to Cart | Checkout, Place Order | JSON path `$.quote_id` | |

## Risk Flags
- {e.g. "No logout transaction — session cleanup behavior under load is untested"}
- {e.g. "Single thread group — task weighting for a mixed workload will need to be decided with the user, not inferred"}

## Suggested Next Step
This analysis is saved alongside a machine-readable `analysis.json` with
the same findings. If you'd like this turned into a runnable Locust
project (with CI/CD, reports, and a QA execution guide), the
performance-engineering-orchestrator skill can take it from here using
that file as a starting point instead of re-parsing the recording.
```

## The machine-readable handoff file (`analysis.json`)

```json
{
  "source_file": "checkout_recording.jmx",
  "format": "jmx",
  "platform": "Magento / Adobe Commerce",
  "auth_model": "form_login",
  "business_flows": [
    {
      "flow_name": "Guest Checkout",
      "steps": [
        {
          "step_name": "Login",
          "method": "POST",
          "path": "/customer/account/loginPost/",
          "hit_count": 1,
          "think_time_ms": 1000
        }
      ]
    }
  ],
  "parametrization_candidates": [
    {
      "field": "username",
      "observed_in": "Login",
      "sample_value": "testuser1@example.com",
      "recommended_column": "username",
      "notes": "only 1 sample observed"
    }
  ],
  "correlation_candidates": [
    {
      "field": "form_key",
      "produced_by": "Any page load",
      "consumed_by": ["Login", "Add to Cart", "Checkout"],
      "extraction_strategy": "regex",
      "expression": "form_key.*?value=\"(.+?)\""
    }
  ],
  "risk_flags": [
    "No logout transaction",
    "Single thread group — task weighting needs user input"
  ]
}
```

Keep the field names in `analysis.json` stable — they're designed to line
up with what `performance-engineering-orchestrator`'s Unified Recording
Model expects (business transactions, auth model, platform, correlation
extractors, parameterized fields), so a handoff doesn't require translating
between two different vocabularies.

## The starter test-data CSV

One column per parametrization candidate from the table above, 3-5
synthetic rows — not a full data set, just enough that the file is
immediately openable and obviously shows the intended shape:

```csv
username,password,sku,quantity,coupon_code
testuser1@example.com,Password123!,SKU12345,1,SAVE10
testuser2@example.com,Password456!,SKU67890,2,SAVE10
testuser3@example.com,Password789!,SKU12345,3,
```

If the recording clearly involves more than one distinct entity (e.g. a
list of shoppers and a separate catalog of products they search for), split
into `users.csv` / `products.csv` rather than forcing everything into one
wide file — match however the parametrization candidates naturally group
in Step 3, don't force a fixed number of files.
