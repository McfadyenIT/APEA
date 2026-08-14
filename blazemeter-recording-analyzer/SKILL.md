---
name: blazemeter-recording-analyzer
description: >
  Analyzes a BlazeMeter recording (.jmx, .har, or BlazeMeter/Taurus .yaml)
  and produces a Business Flow & Data Parametrization insights report —
  plus a machine-readable analysis.json. Pure analysis only: no Locust code,
  CI/CD, or test framework generation. Use whenever the user uploads or
  references a .jmx/.har/.yaml recording and wants to understand it first —
  e.g. "analyze this BlazeMeter recording", "what business flows are in
  this JMX", "what should I parameterize", "find correlation candidates",
  "map out the user journey", "readiness review", "parametrization plan",
  or "before we automate this, what's in it". If the user instead wants a
  runnable Locust project or CI/CD pipeline generated, that's
  performance-engineering-orchestrator or locust-performance-script-generator
  — offer to run this skill first so those get a head start from the
  analysis.json produced here.
---

# BlazeMeter Recording Analyzer

## Why this exists

Before anyone writes a line of load-test code, someone has to actually
understand the recording: which requests are real business steps and which
are static-asset or analytics noise, what order the real steps happen in,
and — critically — which values have to come from test data per virtual
user versus which values the server hands back mid-session and must be
captured and replayed. Skip this step and the resulting test either doesn't
represent real usage, or breaks on the second iteration because a session
token, cart ID, or CSRF value got hardcoded. This skill produces that
upstream understanding as a standalone deliverable, so it's useful on its
own even when nobody is building a Locust project yet.

## Architecture: deterministic parsing, then reasoning — never mixed

This skill deliberately splits into two layers, and the steps below follow
that split:

```
Recording (JMX / HAR / YAML)
        │
        ▼
scripts/parse_recording.py   ← deterministic (Python): parse + noise-filter
        │
        ▼
parsed_recording.json        ← the stable contract between the two layers
        │
        ▼
You (Claude), reasoning       ← business flow naming, parametrization vs.
        │                       correlation classification, risk flags
        ▼
Report + analysis.json
```

The reason for the split: a large recording can easily contain thousands of
static-asset and analytics requests alongside a few dozen real business
calls. Handing all of that raw XML/JSON to an LLM every time burns tokens,
slows things down, and produces less consistent results than a script that
mechanically extracts and filters the same way every run. So Python owns
everything deterministic — XML/JSON/YAML parsing, noise filtering,
normalizing three formats into one shape — and you never need to
understand JMX or HAR syntax directly; you always reason over the same
normalized JSON regardless of what format the recording started in. This
also means support for a new recording format (Postman, OpenAPI, a browser
trace) later would only require a new parser function, not a rewrite of
the reasoning steps below.

What Python does **not** do: decide what a business flow is called, which
values are test data versus session-generated, or what's risky about the
recording. Those need judgment about intent, not just structure — that's
your job, in Steps 2 through 4.

## Step 0 — Locate the recording and confirm scope

Accept `.jmx`, `.har`, or BlazeMeter/Taurus `.yaml`/`.yml`. If the user
attaches more than one, ask whether they want each analyzed separately or
treated as one combined journey (common when a login flow and a checkout
flow were recorded separately). If the file extension is ambiguous or
missing, the parsing script below detects format from content, so don't
block on that.

## Step 1 — Parse deterministically, don't eyeball the raw file

Run `scripts/parse_recording.py` against the recording:

```bash
python scripts/parse_recording.py <path-to-recording> --out parsed_recording.json
```

Walking a JMX's nested `hashTree` XML, grouping HAR entries by page, and
reading a Taurus YAML's `execution`/`scenarios` blocks are mechanical,
format-specific chores — a script does this reliably every time and
normalizes all three formats into one shape, so the rest of the analysis
doesn't care which format the recording started in. If PyYAML is missing
for a `.yaml` recording, install it with
`pip install pyyaml --break-system-packages` and rerun.

The script already filters out static assets (css/js/images/fonts) and
known third-party noise (analytics, tag managers, CDNs) and reports how many
it dropped. Read `references/parsing.md` if the script's output looks wrong
for a particular recording (e.g. an unusual JMX plugin element it doesn't
recognize) — it documents the raw element/field mappings so you can extract
what's missing by hand from the source file.

`parsed_recording.json` gives you, per request: `label`, `method`, `path`,
`host`, `page_group` (the JMX ThreadGroup/TransactionController, HAR page,
or YAML scenario it came from), `headers`, `body`, `think_time_ms`,
`extractors` (regex/JSONPath extractors already declared in the recording,
if any), and `hit_count`. It also reports `platform_guess` and
`auth_model_guess`.

## Step 2 — Group requests into named Business Flows

This is the part that needs judgment, not a script. Take the `page_group`
boundaries as a starting hint, but rename and regroup around what a real
user is actually doing — "Login", "Search for Product", "View Product
Detail", "Add to Cart", "Apply Coupon", "Checkout: Shipping", "Checkout:
Payment", "Place Order" reads far better than raw endpoint paths, and it's
what makes the report useful to a human who didn't record the traffic
themselves.

For each business flow step, capture: the business-meaningful name, the
underlying method + path, `hit_count` (use it to infer relative weight if
the recording has more than one flow), think time, and whether it's a page
render or an API call. Note the overall shape too: is this a single linear
journey, or several independent scenarios/thread groups representing
different user types?

Also flag structural things while you're looking at the flow: is there a
logout/session-end step, or does the recording just stop mid-session? Is
there only one thread group / scenario (meaning you can't infer realistic
task weighting without asking the user)? Was a HAR recorded against
`localhost` or a `.local` domain (won't work against a real environment)?

## Step 3 — Classify every dynamic-looking value

This is the core deliverable, and the distinction between the two buckets
below is the single most important thing this skill needs to get right —
mixing them up is exactly what makes a generated load test hardcode a value
that expires and breaks after one run:

**Parametrization candidates** — values that represent test data varying
per virtual user or iteration, and should be *supplied* from a CSV/data
file rather than replayed verbatim: usernames, passwords, SKUs, quantities,
shipping addresses, search keywords, coupon codes, payment method
selection. If the recording only shows one example (one username, one
SKU), that's still a parametrization candidate — just note that only one
sample was observed and more variety should be added before load testing.

**Correlation candidates** — values the server generates or returns during
the session that must be *extracted* from a prior response and reused, not
supplied: session tokens/cookies, CSRF or `form_key` values, cart/quote
IDs, order IDs, OAuth bearer tokens, any ID that only appears after a
create/checkout call. These cannot come from static test data — hardcoding
them is what breaks replay after the first successful run.

Read `references/parametrization_catalog.md` for the detection patterns
(regexes, common field names, platform-specific conventions for Magento,
SFCC, Mirakl, generic REST/GraphQL) used to tell these apart and to fill in
recommended extraction strategy (which prior response, JSONPath vs regex)
for each correlation candidate.

For every candidate captured, record: field name, where it was observed
(which business flow step), a sample value from the recording, which bucket
it belongs to, and the recommended handling (CSV column name for
parametrization; source response + extraction expression for correlation).

## Step 4 — Roll up risk flags

Borrow the same lens a pre-execution readiness review would use: no
logout/session-end transaction, payment or auth tokens likely to expire
before a load test finishes, timestamps baked into request bodies, an ID
that appears hardcoded but only seen once (can't confirm if it's dynamic or
truly static), a single thread group/scenario, excessive static-asset noise
suggesting the recording captured more than intended. These aren't blockers
— they're things the person building the actual test next needs to know
about.

## Step 5 — Write the deliverable

Three artifacts, all go to the user's output location:

**The report** (`.docx` via the `docx` skill, unless the user asked for
plain Markdown). Follow the structure in `references/report_template.md`:
Overview & Source, Business Flow (narrative + table), Data Parametrization
table, Correlation & Dynamic Values table, Risk Flags, and a short
"Suggested Next Step" section. Name the file after the recording or
project (e.g. `checkout_recording_analysis.docx`), not a generic name like
`report.md` or `analysis.md` — a folder with more than one of these should
still be easy to tell apart at a glance.

**`analysis.json`** — the machine-readable handoff artifact, same shape as
`references/report_template.md`'s JSON schema section
(`business_flows[]`, `parametrization_candidates[]`,
`correlation_candidates[]`, `auth_model`, `platform`, `risk_flags[]`). This
is what lets `performance-engineering-orchestrator` or
`locust-performance-script-generator` skip re-deriving the flow model from
scratch if the user asks for a runnable test next — mention this handoff
explicitly when you deliver the report.

**A starter test-data CSV** (or one per distinct entity if the flow clearly
needs more than one — e.g. `users.csv` plus `products.csv` — otherwise a
single `testdata_starter.csv` is fine). One column per parametrization
candidate from Step 3, with 3-5 synthetic sample rows so the file is
immediately usable rather than just documented in a table. This is a
starting point, not a full data set — say so, and note that
`performance-engineering-orchestrator` generates a fuller 20+ row version
when it builds the actual test.

## Step 6 — Offer, don't assume, the next step

This skill's job ends at understanding and insight — it does not generate
Locust code, CI/CD pipelines, or a runnable framework (that's a separate
job; see below). After delivering the report, ask whether the user wants
this turned into an actual Locust project — if so, that's
`performance-engineering-orchestrator`, and pass along `analysis.json` so
it doesn't start from zero.

## Where this fits in a larger platform (not this skill's job today)

If this analysis step ever needs to run as part of a larger automated
pipeline rather than a one-off request, the natural evolution is a parser
service (this skill's Step 1, exposed as an API) feeding a persistent
Claude reasoning layer (Steps 2-4, each independently callable) into a
framework generator and execution/reporting layer — i.e.
`performance-engineering-orchestrator`'s job, consuming this skill's
`analysis.json` as its input contract instead of re-parsing a recording.
Nothing here needs to be built for that to work later; the JSON contract
already established in Step 1 and `references/report_template.md` is what
makes that future split possible without changing how the reasoning steps
work.
