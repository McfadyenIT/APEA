# Load Test Readiness Review — `mixed_workload.yaml`

**Source file:** `blazemeter-recording-analyzer-workspace\sample-recordings\mixed_workload.yaml`
**Format:** Taurus (BlazeMeter) YAML — `execution` + `scenarios` blocks, single JMeter-style engine
**Reviewed by:** Performance engineering readiness pass (pre-build)
**Date:** 2026-07-16

---

## 1. Executive Summary

The recording defines a **mixed workload of two concurrent scenarios** — a read-only "browse" journey and a transactional "buy" (login → add to cart → checkout) journey. It's a small, clean file (55 lines) and structurally valid Taurus, but it is a **skeleton, not a load-test-ready script**. Before building, several parametrization, correlation, and data-management gaps need to be resolved, and a few load-shape assumptions should be validated with the business/stakeholders.

Bottom line: workable as a starting point, but **not directly runnable at scale without a data plan and correlation hardening**.

---

## 2. Flows / Scenarios Identified

### 2.1 `browse` scenario (read-heavy, unauthenticated)
| Step | Label | Method | URL | Notes |
|---|---|---|---|---|
| 1 | Home Page | GET | `https://shop.example.com/` | Entry point, 1s think-time after |
| 2 | App JS | GET | `https://static.shop.example.com/assets/app.js` | Static asset, different host (CDN?), no think-time |
| 3 | Search | GET | `https://shop.example.com/search/results?q=${keyword}` | Parametrized by `${keyword}`, 2s think-time after |

**Load shape:** 100 concurrent users, 60s ramp-up, 10m hold.

### 2.2 `buy` scenario (transactional, authenticated, stateful)
| Step | Label | Method | URL | Notes |
|---|---|---|---|---|
| 1 | Login | POST | `/api/login` | Body: `email`, `password`; extracts `token` via `$.token`; 1s think-time |
| 2 | Add to Cart | POST | `/api/cart/add` | Auth header `Bearer ${token}`; body `sku`, `quantity`; extracts `cartId` via `$.cartId`; 2s think-time |
| 3 | Place Order | PUT | `/api/checkout/${cartId}/place-order` | Auth header `Bearer ${token}`; asserts response contains `orderId`; 1s think-time |

**Load shape:** 20 concurrent users, 30s ramp-up, 10m hold.

This is a **classic checkout state machine**: Login → Cart → Checkout, each step depending on data extracted from the previous one (bearer token carried through both remaining calls; cart ID carried into the final URL). It's the only scenario with correlation and assertions — appropriately, since it's the only one with dynamic server-generated identifiers.

### 2.3 Combined workload shape
Both scenarios run **concurrently** in the same execution (not sequential phases): 100 browse users + 20 buy users = 120 concurrent VUs at peak, roughly an 83/17 split, which is a reasonable "mostly window-shoppers, some buyers" e-commerce ratio — but worth confirming against real production analytics (funnel conversion rate) rather than assuming.

---

## 3. Correlation Already Present (good news)

- **`token`** — extracted from Login response (`$.token`), reused in both Cart and Checkout requests as `Authorization: Bearer ${token}`. This is correctly wired.
- **`cartId`** — extracted from Add-to-Cart response (`$.cartId`), reused as a path segment in the checkout URL. Correctly wired.
- One assertion exists: Place Order response must `contain: orderId` — a reasonable functional-correctness check to keep in the load test as a pass/fail signal per request.

No broken correlation was found in the two dynamic values that are present. That said, this is a **hand-authored/simplified recording**, not a raw browser-proxy capture — there's no cookie manager, no CSRF token, no session ID, no `Content-Type` headers, and no dynamic values in the `browse` scenario at all, which is unusual for a real e-commerce site and suggests this YAML was curated as a sample rather than pulled straight from a live session. That's fine for a "sample recording," but expect a real proxy capture to have materially more correlation work than this.

---

## 4. Parametrization Plan

### 4.1 Variables currently referenced (undefined in file — need a data source)
| Variable | Used in | Suggested source | Notes |
|---|---|---|---|
| `${keyword}` | Search (browse) | CSV file, e.g. `search_terms.csv`, one column `keyword` | Should reflect realistic query distribution (mix of popular/short-tail and long-tail terms) to exercise search relevance/caching realistically |
| `${email}` | Login (buy) | CSV file, e.g. `users.csv`, columns `email,password` | **Must** be pre-provisioned test accounts, not production data. Needs enough unique rows to avoid session/account collisions across concurrent buy VUs (20 concurrent × iterations over 10m — plan row count accordingly, or accept controlled reuse if the login endpoint tolerates concurrent sessions per account) |
| `${password}` | Login (buy) | Same CSV as email, paired per row | Should be masked/secured — plaintext credentials in a CSV checked into a repo is a security concern; recommend flagging via secrets handling or exclusion from run artifacts/logs |
| `${sku}` | Add to Cart (buy) | CSV or CSV column, e.g. `products.csv` (`sku,quantity` or just `sku`) | Should reflect real catalog distribution — mix of in-stock/high-demand SKUs vs. long-tail, and ideally test at least one edge case (out-of-stock, backorder) if the API distinguishes |
| `${quantity}` | Add to Cart (buy) | Same product CSV or a fixed/randomized small integer range (e.g., 1–3) | Low-cardinality — could be a random function instead of full CSV column if variability doesn't need to be tied to SKU |

### 4.2 Variables already correlated (do **not** need a data file — extracted at runtime)
- `${token}` — from Login response
- `${cartId}` — from Add-to-Cart response

### 4.3 Data source design questions to settle before scripting
1. **CSV recycling policy** — with 20 concurrent buy users over a 10-minute hold, how many login iterations per user should we expect, and do we have enough unique `email`/`password` rows to avoid re-using the same account simultaneously (unless the app explicitly supports concurrent sessions per account)?
2. **Data partitioning** — should each simulated user get a *unique, non-overlapping* slice of the CSV (recommended for stateful checkout flows to avoid cart/account collisions), or is shared/random pull acceptable?
3. **Keyword distribution** — is there an existing search-term frequency report (e.g., from analytics/Splunk) we should sample from instead of a flat/arbitrary list?
4. **SKU/catalog realism** — should SKUs be pulled live from a product export, or is a fixed curated list sufficient for this test's goals?
5. **Environment-specific values** — are `shop.example.com` / `static.shop.example.com` placeholders that need to be swapped for the actual target environment (staging/perf) before execution?

---

## 5. Gaps & Risks to Resolve Before Building

| # | Gap | Why it matters |
|---|---|---|
| 1 | **No data sources defined in the YAML** (`data-sources:` block absent) | All five `${var}` placeholders above are currently undefined — the test will fail or send literal `${keyword}` etc. to the server as-is if run today. |
| 2 | **No assertions on `browse` requests or on Login/Add-to-Cart** | Only "Place Order" has a content assertion. Without response-code/content checks elsewhere, a silently failing Login or Add-to-Cart (e.g., HTTP 200 with an error body) would go undetected and cascade into checkout failures without being flagged at the source. |
| 3 | **No explicit `assert` on HTTP status codes anywhere** | Default JMeter/Taurus behavior may only flag non-2xx as an error depending on sampler config; worth being explicit, especially for the checkout PUT. |
| 4 | **Plaintext credentials pattern** | `email`/`password` as body fields imply a data file with live-looking credentials; needs a secure handling/exclusion plan for CI artifacts and reports. |
| 5 | **No think-time on "App JS" request** | Static asset request has no think-time — fine if intentional (simulates parallel browser resource fetch), but confirm this is deliberate and not an oversight, since it's the only request without one. |
| 6 | **No ramp-down / graceful stop defined** | Only `ramp-up` and `hold-for` are set; no ramp-down means all VUs may stop abruptly at the end of hold, which can distort the tail of the results (in-flight requests, error spikes at cutoff). |
| 7 | **No pacing / throughput shaping (RPS) limits** | Load is concurrency-driven only; if the goal includes hitting a specific requests/sec target (rather than just a VU count), that's not modeled here. |
| 8 | **No cookie/session manager, no CSRF/anti-bot token handling** | Real site likely has session cookies and possibly CSRF tokens on POST/PUT; absence suggests this is a simplified sample — expect more correlation work on the actual production recording. |
| 9 | **No response-time thresholds / pass-fail criteria (`criteria:` block) defined in the YAML** | Nothing here encodes SLAs (e.g., p95 < 2s) that would let the test itself fail the build in CI. |
| 10 | **Two different hosts in `browse`** (`shop.example.com` vs `static.shop.example.com`) | Confirm both are in-scope for load generation (CDN-fronted static assets are sometimes excluded from load tests to avoid skewing results toward third-party/CDN infrastructure not under test). |
| 11 | **Load ratio (100 browse : 20 buy) not tied to a stated business goal** | Confirm this ratio against actual conversion-funnel data or a specific test objective (e.g., "peak Black Friday traffic mix") rather than treating it as arbitrary. |
| 12 | **No warm-up/steady-state separation beyond ramp-up** | 60s/30s ramp-ups into a 10m hold is reasonable for a smoke/moderate test, but confirm this matches the actual test objective (soak, spike, stress, or baseline) since duration and shape should be chosen to match a specific question being asked. |

---

## 6. Suggested Next Steps (once this review is accepted)

1. Confirm target environment hostnames (replace `example.com` placeholders if needed).
2. Source or generate CSV data files for: `keyword`, `email`+`password` (test accounts), `sku`(+`quantity`).
3. Decide CSV consumption policy (unique-per-user vs. shared/random) for the `buy` scenario in particular, given its stateful nature.
4. Add assertions (status code at minimum) to Home Page, Search, Login, and Add to Cart requests, not just Place Order.
5. Decide and add a `criteria:` block with pass/fail SLA thresholds if this test needs to gate a CI pipeline.
6. Confirm the 100:20 browse:buy concurrency ratio and total load level against a stated test objective (baseline/stress/soak/spike).
7. Clarify whether static asset requests (`App JS`) and the CDN host should be included in the load model or excluded/simulated differently.
8. Only after 1–7 are settled: proceed to script the Locust project.

---

*No code or test assets were generated as part of this review — analysis only, per request.*
