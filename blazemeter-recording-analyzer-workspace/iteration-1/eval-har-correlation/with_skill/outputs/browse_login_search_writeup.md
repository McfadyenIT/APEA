# BlazeMeter Recording Analysis — browse_login_search.har

## Overview & Source
- Source file: `browse_login_search.har`, format: HAR (HTTP Archive, Chrome DevTools export)
- Platform detected: Generic (no Magento / SFCC / Mirakl / Shopify signal — this looks like a custom/headless REST + JWT storefront: `/api/login`, `/api/cart/add`)
- Auth model detected: JWT (`Authorization: Bearer <token>`), plus a session cookie (`SESSIONID`) set alongside it at login
- Business-relevant requests: 6 (2 noise entries filtered out — a CSS asset on `static.shop.example.com` and a Google Analytics beacon)
- Scenarios / thread groups: none (HAR has no thread-group concept) — this is a single recorded session/journey, not multiple weighted user types

## Business Flow

This recording captures a single linear journey: a user lands on the home
page, bounces through one stray legacy-URL redirect, logs in, searches for
"laptop", opens the one product the search returned, and adds it to the
cart. The recording stops immediately after Add to Cart — there is no
checkout, no payment step, no order confirmation, and no logout/session-end
step.

| # | Business Step | Method + Path | Hit Count | Think Time | Notes |
|---|---|---|---|---|---|
| 1 | Browse Home Page | GET / | 1 | — (first request) | |
| 2 | Legacy URL Redirect | GET /old-home | 1 | 2.1s | 302 → `/`; trivial bounce back to the home page already visited in step 1, not an SSO/auth hop |
| 3 | Login | POST /api/login | 1 | 2.4s | Body: email + password. Response: `token` (JWT), `customerId`, and a `Set-Cookie: SESSIONID=...; HttpOnly` |
| 4 | Search for Product ("laptop") | GET /search/results?q=laptop&sort=relevance | 1 | 2.5s | Sent with `Authorization: Bearer <token>`. Response returns SKU `LAP-2001` |
| 5 | View Product Detail (LAP-2001 / UltraBook 14) | GET /product/LAP-2001 | 1 | 2.2s | SKU in the path matches the SKU just returned by search |
| 6 | Add to Cart | POST /api/cart/add | 1 | 3.2s | Body: sku + quantity. Response returns `cartId` — recording ends here |

## Data Parametrization
Values that should come from test data (a CSV row per virtual user), not be replayed verbatim.

| Field | Observed In | Sample Value | Recommended Column | Notes |
|---|---|---|---|---|
| email | Login | jane.doe@example.com | username | Only 1 sample observed — add variety |
| password | Login | Secret123 | password | Only 1 sample; plaintext exactly as the app expects — parametrize verbatim |
| q (search term) | Search for Product | laptop | search_keyword | Only 1 sample; `sort=relevance` looks like a fixed UI default, not worth parametrizing |
| sku | Add to Cart (also in Search response body and PDP path) | LAP-2001 | sku | Only 1 sample. See Correlation section below — this value is *derived* from the search response within the session, so decide deliberately whether to hardcode it as test data or correlate it from search results |
| quantity | Add to Cart | 1 | quantity | Only 1 sample (always 1) — add variety |

## Correlation & Dynamic Values
Values the server generates mid-session that must be extracted from a prior response and replayed — hardcoding these breaks the test after the first run.

| Field | First Produced By | Consumed By | Extraction Strategy | Notes |
|---|---|---|---|---|
| `token` (JWT) | Login response body (`$.token`) | Search, Product Detail, Add to Cart (all via `Authorization: Bearer`) | JSON path `$.token` from the Login response | **Highest-priority fix.** Found via the Login entry's `response_snippet`. The literal token string is reused verbatim as the Authorization header on the next 3 requests — hardcode it and the script works once, then returns 401 on every request after Login on every later run/iteration once the JWT expires. |
| `SESSIONID` cookie | Login response `Set-Cookie` header | Not explicitly re-sent as a request header anywhere in this HAR | Regex on `Set-Cookie`: `SESSIONID=([^;]+)` | Found via `response_set_cookie` on the Login entry. HttpOnly. Never shown being resent in the capture because the browser's cookie jar handled it automatically — a load-testing tool won't do this for you; extract and replay it explicitly or server-side session state may not match what the bearer token implies. |
| `cartId` | Add to Cart response body (`$.cartId`) | None observed (recording ends here) | JSON path `$.cartId` | Found via `response_snippet` on the Add to Cart entry (`cart_9f8e7d`). No consumer in this capture, but this is exactly the kind of ID any Checkout/Place Order step layered on top would need — don't hardcode it if the journey is extended. |
| `customerId` | Login response body (`$.customerId`) | None observed | JSON path `$.customerId` | Found via `response_snippet` on the Login entry (`98213`). Captured but unused in this recording — may matter for account/order flows not present here. |

## Risk Flags
- No logout/session-end step — the recording stops mid-session right after Add to Cart; checkout, payment, and logout are all untested.
- The JWT bearer token and SESSIONID cookie are the biggest replay risk: the JWT is a literal string reused across 3 requests in the raw capture — hardcode it and the test passes once, then fails with 401s on every subsequent run once it expires.
- SESSIONID is never shown being resent as a request header in this HAR (browser auto-handled it) — a replay tool must capture it from `Set-Cookie` and resend it explicitly.
- Single recorded session — no thread groups/scenarios to infer relative task weighting, and only one sample each of username, password, search keyword, SKU, and quantity.
- `cartId` and `customerId` are captured but never consumed later in this recording — can't confirm downstream usage until the flow is extended past Add to Cart.
- A 302 redirect (`/old-home` → `/`) appears mid-session; it looks like a trivial legacy-URL bounce rather than an SSO hop, but is called out explicitly per the parsing rule that redirects are never auto-treated as noise.
- Platform detected as Generic — a custom/headless REST + JWT storefront, not Magento/SFCC/Mirakl/Shopify, so none of those platforms' specific correlation conventions apply.
- Recording is short (~12 seconds, one session) — token/session lifetime is inferred from the JWT's format, not directly observed failing.

## Suggested Next Step
This analysis is saved alongside a machine-readable `analysis.json` with the
same findings (business flow, parametrization candidates, correlation
candidates, risk flags). If you'd like this turned into a runnable Locust
project (with CI/CD, reports, and a QA execution guide), the
`performance-engineering-orchestrator` skill can take it from here using
`analysis.json` as a starting point instead of re-parsing the HAR. At minimum,
before any replay: extract and inject the `token` and `SESSIONID` correlation
values dynamically instead of hardcoding them, and expand the starter test
data (`testdata_starter.csv`) beyond the single observed sample per field.
