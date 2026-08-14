# BlazeMeter Recording Analysis — mixed_workload.yaml

## Overview & Source
- Source file: `mixed_workload.yaml`, format: BlazeMeter/Taurus YAML
- Platform detected: Generic (no Magento/SFCC/Mirakl/Shopify signature found in hosts or paths)
- Auth model detected: JWT (Bearer token) — first seen on the "Add to Cart" request's `Authorization: Bearer ${token}` header
- Business-relevant requests: 5 (1 noise entry filtered out — `App JS`, a static `.js` asset)
- Scenarios: **2** — `browse` (concurrency 100, ramp-up 60s, hold-for 10m) and `buy` (concurrency 20, ramp-up 30s, hold-for 10m)

**Note on method:** the bash/sandbox tool was unavailable in this session, so `scripts/parse_recording.py` could not literally be executed. Because the YAML is only 55 lines, it was hand-traced against the script's actual logic (`parse_yaml`, `is_noise`, `compute_hit_counts`, `guess_auth_model`) rather than eyeballed — see `analysis.json`'s `parsing_metadata.parsing_method` for detail.

## Business Flow

This recording is **not** a single journey — it declares two separate `execution` blocks, each pointing at its own `scenarios` entry, which per the skill's own YAML-parsing notes means two distinct user populations should be reported separately, not merged. Recognizing that separation is the main structural fact about this recording, so it's kept front and center below rather than flattened into one five-step flow.

### Flow 1: Browse (Guest) — scenario `browse`, concurrency 100

A short, unauthenticated browsing journey: land on the homepage, then run a single search. No login, no cart activity, no logout.

| # | Business Step | Method + Path | Hit Count | Think Time | Notes |
|---|---|---|---|---|---|
| 1 | Home Page | GET / | 1 | 1.0s | |
| 2 | Search | GET /search/results?q={keyword} | 1 | 2.0s | `keyword` is a bare `${keyword}` placeholder, no literal value recorded |

### Flow 2: Buy (Authenticated Purchase) — scenario `buy`, concurrency 20

A short authenticated purchase journey: log in, add one item to cart, place the order. No logout/session-end step.

| # | Business Step | Method + Path | Hit Count | Think Time | Notes |
|---|---|---|---|---|---|
| 1 | Login | POST /api/login | 1 | 1.0s | Declares `extract-jsonpath: token: $.token` |
| 2 | Add to Cart | POST /api/cart/add | 1 | 2.0s | Sends `Authorization: Bearer {token}`; declares `extract-jsonpath: cartId: $.cartId` |
| 3 | Place Order | PUT /api/checkout/{cartId}/place-order | 1 | 1.0s | Sends `Authorization: Bearer {token}`; `cartId` is embedded in the path itself; asserts response contains `orderId` |

**Overall shape:** two independent scenarios, not one linear journey with branches. `browse` runs at 5x the concurrency of `buy` (100 vs 20) — that ratio is a real signal from the recording about relative population size (many more browsers than buyers) and should carry through into how a future load test weights the two scenarios, rather than being re-guessed.

## Data Parametrization

Values that should come from test data (a CSV row per virtual user), not be replayed verbatim.

| Field | Observed In | Sample Value | Recommended Column | Notes |
|---|---|---|---|---|
| keyword | Search (Browse flow) | `${keyword}` — placeholder only | search_keyword | No literal sample text exists anywhere in the recording, unlike the other fields below — there isn't even one example to judge variety from. Source real search terms before load testing. |
| email | Login (Buy flow) | `${email}` — placeholder only | username | Only the placeholder is present; needs real credentials created/sourced with variety across virtual users. |
| password | Login (Buy flow) | `${password}` — placeholder only | password | Same as email — placeholder only, no literal value recorded. |
| sku | Add to Cart (Buy flow) | `${sku}` — placeholder only | sku | Needs a real product/SKU catalog sample before load testing. |
| quantity | Add to Cart (Buy flow) | `${quantity}` — placeholder only | quantity | Needs representative quantities (e.g. 1–3). |

Note: this recording is unusual in that it uses `${var}` placeholders directly in the request bodies/URLs rather than showing literal recorded values (e.g. a real email address or SKU) — it reads more like a pre-templatized script than a raw capture. That doesn't change the parametrization classification, but it does mean there is zero real sample data to seed test-data variety from; every value above needs to be sourced from scratch.

## Correlation & Dynamic Values

Values the server generates mid-session that must be extracted from a prior response and replayed — hardcoding these breaks the test after the first run.

| Field | First Produced By | Consumed By | Extraction Strategy | Notes |
|---|---|---|---|---|
| token | Login response, JSON path `$.token` (declared in the recording) | Add to Cart, Place Order (both via `Authorization: Bearer {token}`) | JSON path `$.token` | Already declared as an extractor in the recording itself — the recorder pre-identified this as a correlation point. |
| cartId | Add to Cart response, JSON path `$.cartId` (declared in the recording) | Place Order (embedded directly in the request path: `/api/checkout/{cartId}/place-order`) | JSON path `$.cartId` | Also pre-declared. Because it only ever appears after Add to Cart's response and then reappears in the very next request's path, this is a textbook correlation ID, not test data. |

Important distinction to flag explicitly: `token` and `cartId` use the exact same `${...}` syntax in the YAML as the parametrization fields above (`${email}`, `${sku}`, etc.), but they are **not** parametrization candidates — the recording's own `extract-jsonpath` blocks show they're generated server-side (Login and Add to Cart responses respectively) and must be captured and replayed, never supplied from a CSV. Mixing these two up is exactly the failure mode this kind of analysis exists to catch.

## Risk Flags

- No logout/session-end step in either scenario — the Buy flow ends at Place Order with no explicit session termination.
- The bearer token is captured once at Login and reused across Add to Cart and Place Order; with `hold-for: 10m` on the buy scenario, confirm the server-side token TTL so the test doesn't start failing mid-run on token expiry.
- The `keyword` field has no literal sample at all in the recording (just the bare placeholder) — worse than the other fields, which at least show the placeholder is where a real value belongs; there's nothing to anchor even a first guess at realistic search terms.
- No CSV/data file is declared inside the YAML (`csv_datasets` is empty) — all five parametrization fields are referenced only as `${var}` placeholders with no accompanying data source in the recording, so the starter CSVs are built from scratch, not adapted from something already present.
- Two independent scenarios with different concurrency (browse: 100, buy: 20) are two separate simulated populations, not one flow — a combined test needs to run both scenarios concurrently at roughly their recorded 5:1 ratio, not merge the steps into a single flow or arbitrarily pick one.
- The recording is very short (2 steps in Browse, 3 in Buy) — no product detail page, cart review, or shipping/payment step appears before Place Order. Confirm with the user whether this is the complete intended journey or a deliberately trimmed sample.
- `platform_guess` is "Generic" — no Magento/SFCC/Mirakl/Shopify signature detected in hosts or paths, so no platform-specific correlation conventions (e.g. Magento's `form_key`) apply automatically here; treat token/cartId handling as generic REST until told otherwise.

## Suggested Next Step

This analysis is saved alongside a machine-readable `analysis.json` with the same findings (`business_flows`, `parametrization_candidates`, `correlation_candidates`, `risk_flags`), plus two starter test-data files (`browse_testdata.csv`, `buy_testdata.csv`) — split by flow rather than combined into one file, since the Browse and Buy scenarios need different fields and represent different virtual-user populations.

No load test, Locust script, or framework has been built here — this is analysis only, as scoped. If a runnable Locust project is wanted next (with CI/CD, reporting, and a fuller 20+ row data set), the performance-engineering-orchestrator (or locust-performance-script-generator) skill can take it from here using `analysis.json` as a starting point instead of re-parsing the recording.
