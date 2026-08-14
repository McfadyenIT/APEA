---
name: locust-performance-script-generator

description: >
  Enterprise-grade Locust Performance Testing Skill. Use whenever the user wants to generate Locust
  scripts, run performance/load/stress tests, discover APIs, set up CI/CD for performance testing,
  or generate HTML/Excel reports. Triggers on: "performance test", "load test", "stress test",
  "locust script", "generate locustfile", "workload model", "performance test plan", "transaction
  mix", "user division", "NFR requirements", "CI/CD pipeline for perf", "performance report",
  "HTML dashboard", "Excel report after load test". Claude crawls the target URL via Chrome MCP,
  discovers APIs and user journeys, generates production-ready Locust scripts, wires up CI/CD
  (GitHub Actions + Jenkins + Docker), and auto-generates an HTML dashboard + Excel workbook after
  every test run - all written to the user's VS Code workspace.
---

# Locust Performance Script Generator

## Role
Behave as a Senior Performance Engineer. Never generate generic or placeholder code.
Every script and document must be grounded in actual discovered application behavior.

---

## Step 0 — Collect Prerequisites

Ask the user for the following before proceeding:

**Required:**
1. **Target URL** — the website to test
2. **VS Code workspace path** — where to write output files (e.g. `/Users/john/projects/perf-tests`)
3. **Project name** and **Client name** — for the formal test plan header

**Optional (ask, then proceed with defaults if not provided):**
4. **Credentials** — username/password if login flows are needed
5. **Load profile** — smoke / load / stress / spike / soak / volume / scalability, or "all"
6. **Expected concurrent users** — normal and peak counts (default: 100 normal / 500 peak)
7. **User division** — % breakdown by user type (e.g. End User 60%, Admin 10%)
8. **Test region** — geographic location for test execution (default: same as app host)
9. **Environment details** — test vs. production server config (CPU, RAM, DB size)
10. **Schedule dates** — planned dates for each execution phase

Store collected values — they populate the formal test plan.

---

## Step 1 — Browser Crawl via Chrome MCP

Use `Claude in Chrome` tools to crawl the application. **Mandatory — do not generate
scripts based on assumptions alone.**

### 1.1 Validate Target
```
navigate to target_url
capture: status_code, final_url (after redirects), page title
detect: CDN headers (X-Cache, CF-Ray, X-Amz-Cf-Id), WAF signatures, Cloudflare
```

### 1.2 Technology Detection
Read page source / DevTools to detect:
- **Frontend**: React, Angular, Vue, Next.js, Nuxt, Magento PWA, SFCC, Shopify
- **Backend**: Magento, SFCC, Shopify, Hybris, Mirakl, WooCommerce, Custom
- **API type**: REST, GraphQL, SOAP, Hybrid

### 1.3 Full Page Inspection
Navigate to each page and capture network traffic:
- Homepage → Category → PLP → PDP
- Search (submit a keyword, capture results)
- Cart page
- Login page (if credentials provided: complete login and capture auth tokens)
- Checkout page (if credentials provided)

At each page use `read_network_requests` to capture:
- All XHR/Fetch API calls (method, URL, request body, response status)
- GraphQL operations (operationName, query/mutation, variables)
- Auth headers and cookies

### 1.4 Build API Inventory
For every API request observed:
```json
{
  "name": "<descriptive name>",
  "method": "GET|POST|PUT|PATCH|DELETE",
  "endpoint": "<path or full URL>",
  "headers": { "<key>": "<value or DYNAMIC>" },
  "request_body": {},
  "response_codes": [200],
  "dynamic_params": ["<values that must be correlated>"]
}
```
**Never invent APIs not observed.**

This inventory — along with the actual search terms, product/category identifiers, and form fields seen during the crawl — is what Step 5.2's `testdata.csv` must be built from. Two clients on different domains (e.g. a general retailer vs. a pharmaceutical or B2B site) should never produce a similar-looking `testdata.csv`, because their actual crawled products, categories, and search terms are different. If your generated CSV would look the same regardless of which site was crawled, you've fallen back to a template instead of using what Step 1 found — go back and pull the real values.

---

## Step 2 — Authentication Discovery

| Model | Signals |
|---|---|
| Anonymous | No auth headers, no login redirect |
| Form Login | HTML form with username/password |
| SSO | Redirect to Okta / Azure AD / Google |
| JWT / Bearer | `Authorization: Bearer <token>` in headers |
| Session Cookie | `Set-Cookie` with session/PHPSESSID/JSESSIONID |

Generate extraction logic for whatever is found. Never hardcode tokens.

---

## Step 3 — Correlation Discovery

Identify all dynamic values: Session ID, CSRF Token, JWT, Cart ID, Product ID, Category ID,
Order ID, Customer ID, Address ID, Payment Token, Coupon ID.

**Rules:**
- Every dynamic value must be extracted, stored, and reused
- Never: `cart_id = "12345"` ❌  Always: `cart_id = response.json()["cartId"]` ✓

---

## Step 4 — User Journey Mapping

Based on discovered pages and APIs, map applicable journeys. Build the journey list and business-flow steps below from what was actually crawled for *this* client — the table and step lists here are illustrative of the Magento URL shape and labeling convention, not a fixed script every project should reproduce. A different client's business flow (different journey count, different steps, different platform) should produce a visibly different journey map and task set. If two different clients' `journey_map.md` end up describing the same flows in the same order, the crawl output isn't being used.

**Transaction labels MUST match these names exactly** — they align with the report generator SLA config (adapt the label set itself to the client's actual discovered pages; the Magento example below shows the naming convention, not a mandatory fixed list):

| Label | Method | Path Pattern | Notes |
|---|---|---|---|
| `Login Page` | GET | `/uk/customer/account/login/` | Auth flow entry |
| `Home Page` | GET | `/uk/` | Anonymous browse |
| `Search Keyword` | GET | `/uk/catalogsearch/result/?q=<keyword>` | Parameterised from testdata.csv |
| `PLP` | GET | `/uk/<category-path>.html` | Category listing |
| `PDP` | GET | `/uk/buy/<product-slug>/<id>.html` | Product detail |
| `Add Basket Action` | POST | `/uk/checkout/cart/add/uenc/<encoded>/product/<id>/` | Requires login + form_key |
| `Cart Page` | GET | `/uk/checkout/cart/` | Requires login |

| # | Journey | Steps |
|---|---|---|
| 1 | Anonymous Browse | Home Page → Search Keyword → PLP → PDP |
| 2 | Authenticated Browse + Cart | Login Page → Home Page → Search Keyword → PLP → PDP → Add Basket Action → Cart Page |
| 3 | Search Flow | Home Page → Search Keyword → PLP → PDP |
| 4 | Cart Management | Login Page → Cart Page → Add Basket Action |

**Task weights (tuned to observed JMeter traffic pattern — recompute per client from the actual hit counts observed in Step 1, don't reuse these numbers by default):**
```python
@task(30) def home_page(self): ...
@task(25) def search_keyword(self): ...
@task(20) def plp(self): ...
@task(15) def pdp(self): ...
@task(7)  def add_basket_action(self): ...
@task(3)  def cart_page(self): ...
```

**Magento-specific (detected from JMeter data):**
- Extract `form_key` CSRF token from login page HTML before any POST
- `Add Basket Action` body: `product=<id>&qty=1&form_key=<extracted>`
- Encode `uenc` parameter as base64 of the referrer PDP URL
- Maintain session cookie across all authenticated requests
- For other platforms: **Magento** GraphQL cart/checkout mutations; **SFCC** Shopper/Basket APIs; **Mirakl** Seller/Offer APIs

---

## Step 5 — Generate Output Files

Write all files to `output_dir`. Total deliverables: **8 files**.

### 5.1 `locustfile.py`
- Import: `from locust import HttpUser, task, between, events`
- Class: `class EcommerceUser(HttpUser):`
- `wait_time = between(2, 5)`
- All requests use `catch_response=True`
- Every task uses `name=` parameter so Locust labels match JMeter transaction names exactly
- Validate status code, response URL pattern, AND a business payload indicator per request
- All dynamic values correlated (never hardcoded)
- Helper methods: `extract_csrf()`, `extract_form_key()`, `extract_cart_id()`, `extract_token()`

**Endpoint validation template — every task must follow this pattern:**
```python
@task(30)
def home_page(self):
    with self.client.get(
        "/uk/",
        name="Home Page",          # MUST match JMeter label exactly
        catch_response=True
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"[Home Page] Expected 200 got {resp.status_code} | {resp.url}")
        elif not resp.url.endswith("/uk/"):
            resp.failure(f"[Home Page] Unexpected URL: {resp.url}")
        else:
            resp.success()

@task(25)
def search_keyword(self):
    kw = self.search_data.pop()     # from testdata.csv
    with self.client.get(
        f"/uk/catalogsearch/result/?q={kw}",
        name="Search Keyword",      # MUST match JMeter label
        catch_response=True
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"[Search Keyword] {resp.status_code} | q={kw} | {resp.url}")
        else:
            resp.success()

@task(7)
def add_basket_action(self):
    # POST requires form_key (Magento CSRF)
    with self.client.post(
        f"/uk/checkout/cart/add/uenc/{self.uenc}/product/{self.product_id}/",
        data={"product": self.product_id, "qty": 1, "form_key": self.form_key},
        name="Add Basket Action",   # MUST match JMeter label
        catch_response=True
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"[Add Basket Action] {resp.status_code} | {resp.url}")
        else:
            resp.success()
```

**Failure log format — include URL, code, and response excerpt:**
```python
resp.failure(f"[{name}] code={resp.status_code} url={resp.url} body={resp.text[:150]}")
```

### 5.2 `testdata.csv`

Build every row from what Step 1 (Browser Crawl) and Step 1.4 (API Inventory) actually observed for **this specific client** — not from the example below, which only illustrates the column schema. If you copy the example row's values (`laptop`, `prod_001`, `cat_electronics`) into a real project, or if two different clients' generated CSVs end up looking alike, you've produced a templated CSV instead of a data-driven one — that's the defect to avoid.

```csv
username,password,search_keyword,product_id,category_id,coupon_code
<synthesized test account — credentials can't come from a real crawl>,<synthesized password>,<a search term actually submitted and observed during the crawl>,<a real product ID seen in a PDP or add-to-cart URL>,<a real category path segment crawled>,<a coupon actually exercised during the crawl, or leave blank>
```

Generate 20+ rows using the real search keywords, product IDs, and category paths captured in `api_inventory.json` during Step 1 — repeat and vary them if fewer than 20 distinct real values were observed, rather than filling the remaining rows with invented values from outside this client's actual product domain (e.g. don't add generic retail terms to a pharmaceutical or B2B site's data just to hit the row count). Only `username`/`password` are expected to be synthesized outright; note that explicitly in `performance_assumptions.md` (Step 5.7) so it's clear which column is synthetic and which are recording-derived. If `coupon_code` was never exercised in the crawl, leave it blank and note the gap in `performance_assumptions.md` rather than inventing a code that will fail at runtime.

### 5.3 `api_inventory.json`
Full API catalog from Step 1.4.

### 5.4 `journey_map.md`
Each journey with steps, APIs used, and correlation points.

### 5.5 `workload_model.md`
Include all of the following tables:

**Transaction Mix Table:**
| Journey | Operation | Peak Volume/Day | Avg Volume/Day | SLA (Response Time) |
|---|---|---|---|---|

**User Division Table:**
| Sr. No | User Type | % of Concurrent Users |
|---|---|---|

**Workload Weight Table:**
| Journey | Weight | Concurrent Users (at 100 VU) | Rationale |

**Load Profiles:**
| Profile | Users | Duration | Ramp | Purpose |
|---|---|---|---|---|
| Smoke | 10 | 2 min | Immediate | Script validation |
| Load | 100 | 10 min | 2 min | Normal load |
| Stress | 500 | 20 min | 5 min | Breaking point |
| Spike | 1000 | 5 min | Immediate | Flash sale |
| Soak | 100 | 8 hr | 2 min | Memory leak detection |
| Volume | 100 | 1 hr | 2 min | Production-scale DB |
| Scalability | 50→500 | Stepped | 50/step | Capacity planning |

### 5.6 `execution_guide.md`
- `pip install locust` command
- All 7 load profile Locust commands with `--headless` flag
- Performance thresholds (P95 per page, error rate < 1%)
- Resource utilization targets per server tier (CPU < 75%, Memory < 60%)
- Monitoring checklist: App servers, DB, Redis, Elasticsearch, CDN, Load Balancer
- Specific counters: CPU (system calls/sec, context switches/sec), Memory (available/used), GC, slow queries, lock waits, Redis eviction rate, CDN hit/miss ratio

### 5.7 `performance_assumptions.md`
- What was observed vs. inferred
- APIs excluded and why
- Known gaps (payment gateway, SSO not crawled)
- Which `testdata.csv` columns are recording-derived vs. synthesized (see 5.2) — call this out explicitly per column, not just in general terms
- Risk register: Description, Impact, Probability, Mitigation
- Special considerations: dedicated environment, production-sized DB, geographic test location, background batch jobs
- Recommended manual validation steps before first run

### 5.8 `performance_test_plan.md`
**Read `references/test_plan_template.md` first**, then generate a fully populated version.

The plan must include all 12 sections:
1. Objective and Purpose
2. Scope (In Scope / Out of Scope)
3. Assumptions & Risks (with Risk Register table)
4. Requirements: Test Types (all 7), Critical Transactions, User Division, Transaction Mix, Expected Users
5. Performance Targets: Throughput TPS table, Response Time P95 table, Resource Utilization table
6. Performance Test Life Cycle: Test Approach, Scenario Definitions, Scripts Index, Workload,
   Test Completion Criteria, Monitoring Strategy, Special Considerations, Environment, Tools
7. Roles and Responsibilities table
8. Schedule and Planning table
9. System Architecture (description + server config comparison)
10. Deliverables table — updated to include CI/CD files and report outputs
11. Acronyms table
12. Sign-Off table

Replace all `{placeholder}` values with real data. Never leave `<TBD>` in the output.

---

### 5.9 CI/CD Pipeline Files

**Read `references/cicd_templates.md` first**, then write these three files:

#### `ci/github-actions.yml` — `.github/workflows/performance-tests.yml`
- `workflow_dispatch` trigger with inputs: profile, users, duration
- Runs on `push` to `main` when `locustfile.py` or `testdata.csv` changes
- Steps: checkout → setup Python → install deps → run Locust → generate report → upload artifacts → PR comment → SLA check
- Secrets: `PERF_TARGET_URL`, `PERF_USERNAME`, `PERF_PASSWORD`
- Passes `--csv=results/locust` and `--html=results/locust_raw.html` to Locust
- Calls `python reports/generate_report.py` after every run
- Publishes `performance_report.html` and `performance_report.xlsx` as artifacts

#### `ci/Jenkinsfile`
- Parameters: PROFILE, USERS, DURATION
- Stages: Setup → Run Performance Test → Generate Reports → Check SLA
- `post { always { publishHTML(...) archiveArtifacts(...) } failure { emailext(...) } }`
- Uses `credentials()` for secrets

#### `ci/Dockerfile`
- Base: `python:3.11-slim`
- Copies `locustfile.py`, `testdata.csv`, `reports/`
- Entrypoint: runs Locust then report generator
- ENV vars: `TARGET_URL`, `LOCUST_USERS`, `LOCUST_SPAWN_RATE`, `LOCUST_RUN_TIME`

#### `requirements-perf.txt`
```
locust>=2.20.0
openpyxl>=3.1.0
jinja2>=3.1.0
```

---

### 5.10 Report Generator — `reports/generate_report.py`

**Copy from `references/generate_report.py`** verbatim to `{output_dir}/reports/generate_report.py`.

**Supported input formats (auto-detected):**
- **JMeter CSV** — raw row-per-request log (columns: `timeStamp`, `elapsed`, `label`,
  `responseCode`, `success`, `bytes`, `sentBytes`, `allThreads`, `URL`, `failureMessage`)
- **Locust CSV** — pre-aggregated stats (`*_stats.csv` + optional `*_stats_history.csv`)

The generator detects format automatically from column headers — no flag needed.

**`performance_report.html`** — Dark-theme interactive dashboard with 4 tabs:
- 8 KPI cards: Total Samples, Failures, Error Rate, Throughput (req/s), Avg/P95 Response, Transactions, SLA Breaches
- Line charts: P50 + P95 over time; Requests/s + Virtual Users + Failures/s
- Bar chart: P95 per transaction vs SLA threshold (red dashed line)
- **Tab 1 — Endpoint Validation**: Transaction, Endpoint URL, Samples, Response Code (actual vs expected), Response Message, Error %, Throughput, Status ✅/❌
- **Tab 2 — Transaction Stats**: all percentiles + URL + Throughput + Response Codes + Messages
- **Tab 3 — SLA Compliance**: breach details
- **Tab 4 — Failures**: full failure log with URL, response code, response message, elapsed

**`performance_report.xlsx`** — 7-sheet workbook:
- **Summary** — KPIs + colour-coded SLA result cell
- **Endpoint Validation** — Transaction, Endpoint URL, Samples, Throughput, Error %, Expected Code, Actual Code, Response Message, Code Valid, Status (PASS/FAIL)
- **Transaction Stats** — all percentiles + SLA P95 + status column
- **Response Codes & Messages** — code distribution per transaction, colour-coded 200=green / non-200=red
- **Time Series** — bucketed rows + embedded P50/P95 line chart
- **Failures** — Transaction, URL, Response Code, Response Message, Elapsed
- **SLA Compliance** — breach details

**SLA thresholds (tuned from observed JMeter data):**

| Transaction | P95 Target | Error Rate |
|---|---|---|
| Login Page | 2000 ms | < 1% |
| Home Page | 500 ms | < 1% |
| Search Keyword | 3000 ms | < 1% |
| PLP | 500 ms | < 1% |
| PDP | 500 ms | < 1% |
| Add Basket Action | 3000 ms | < 1% |
| Cart Page | 5000 ms | < 1% |
| Default | 3000 ms | < 1% |

Edit the `SLA` dict in `generate_report.py` to adjust per project.

**Usage:**
```bash
# JMeter CSV in results/ — auto-detected
python reports/generate_report.py \
  --results-dir results --output-dir reports/output \
  --project "Radwell EU" --target-url "https://example.com" --profile load

# Fail CI build on SLA breach
python reports/generate_report.py \
  --results-dir results --output-dir reports/output \
  --check-sla --fail-on-breach
```

**Transaction labels in `locustfile.py` must match these exactly** so reports align:
`Login Page`, `Home Page`, `Search Keyword`, `PLP`, `PDP`, `Add Basket Action`, `Cart Page`

---

## Step 6 — Write Files to Disk

```bash
mkdir -p {output_dir}/ci
mkdir -p {output_dir}/reports
mkdir -p {output_dir}/reports/output
```

Write all files and confirm each with path and line count.
Total deliverables: **12 files** across root, `ci/`, and `reports/` directories.

---

## Step 7 — Summary Report

```
✅ Locust Performance Test Suite Generated
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Target:        <url>   Platform: <detected>
APIs Captured: <count>  Journeys: <count>
Output Dir:    <output_dir>

Core Test Files:
  locustfile.py               (<N> lines)
  testdata.csv                (20+ rows)
  requirements-perf.txt
  api_inventory.json          (<N> APIs)

Documentation:
  journey_map.md
  workload_model.md           (7 load profiles + transaction mix)
  execution_guide.md
  performance_assumptions.md  (risk register included)
  performance_test_plan.md    (12-section formal plan)

CI/CD:
  ci/github-actions.yml       (GitHub Actions workflow)
  ci/Jenkinsfile              (Jenkins declarative pipeline)
  ci/Dockerfile               (containerised test runner)

Reporting:
  reports/generate_report.py  (HTML + Excel generator)

After each run, reports auto-written to:
  reports/output/performance_report.html  (interactive dashboard)
  reports/output/performance_report.xlsx  (5-sheet workbook)
  reports/output/summary.txt             (CI comment text)

Next Steps:
  1. pip install -r requirements-perf.txt
  2. Review performance_test_plan.md
  3. Smoke test:
     locust -f locustfile.py --users 10 --spawn-rate 2 --run-time 2m \
            --headless --csv=results/locust
  4. Generate report:
     python reports/generate_report.py \
            --results-dir results --output-dir reports/output
  5. Open reports/output/performance_report.html in browser
```

---

## Mandatory Standards Checklist

- [ ] Every request uses `catch_response=True`
- [ ] No hardcoded dynamic values (cart IDs, tokens, session IDs)
- [ ] Test data loaded from `testdata.csv` via `csv.DictReader`
- [ ] `testdata.csv` values (search terms, product IDs, categories) trace back to this client's own Step 1/1.4 crawl output — not a template shared across clients
- [ ] Auth headers maintained across requests
- [ ] CSRF tokens extracted and reinjected where required
- [ ] Task weights assigned (~100 total), computed from this client's observed traffic pattern
- [ ] All platform-specific APIs covered (Magento / SFCC / Mirakl / GraphQL)
- [ ] Failure messages include endpoint + status + response excerpt
- [ ] `performance_test_plan.md` has all 12 sections fully populated
- [ ] Transaction Mix table populated with real data
- [ ] User Division table populated
- [ ] Risk Register has ≥ 4 rows
- [ ] Roles & Responsibilities table complete
- [ ] Scripts Index table lists every Locust task method
- [ ] `ci/github-actions.yml` uses secrets, not hardcoded credentials
- [ ] `ci/Jenkinsfile` has publishHTML and archiveArtifacts in post block
- [ ] Every task uses `name=` parameter matching JMeter transaction label exactly
- [ ] Every task validates response URL pattern, status code, and response message
- [ ] `reports/generate_report.py` copied from `references/generate_report.py`
- [ ] SLA thresholds in `generate_report.py` match project targets
- [ ] All 12 output files written to `output_dir`
