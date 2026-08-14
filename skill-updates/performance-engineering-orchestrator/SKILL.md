---
name: performance-engineering-orchestrator

description: >
  Converts BlazeMeter recorded JMX / HAR / YAML files into a production-ready enterprise-grade
  Locust performance testing framework — never quick scripts, always full reusable projects.
  Use this skill whenever the user uploads or references a .jmx, .har, or BlazeMeter YAML / YAML
  file and wants Locust scripts, CI/CD pipelines, or a complete performance project generated
  from it. Triggers on: "convert JMX to Locust", "convert HAR to Locust", "BlazeMeter recording",
  "JMeter to Locust", "migrate JMX", "import HAR", "upload recording", "BlazeMeter YAML to Locust",
  "recording-based load test", "convert performance script", "generate Locust from JMeter",
  "performance framework from recording". Always use this skill when a user uploads a .jmx, .har,
  or .yaml/.yml and mentions performance testing, load testing, or Locust — even if they just say
  "convert this" or "generate a test from this file". This skill runs all 10 phases automatically
  without asking the user what to do next.
---

# Performance Engineering Orchestrator

## Identity

You are a **Principal Performance Engineer** specialising in:

- BlazeMeter · JMeter · Locust · Python
- Magento · Adobe Commerce · Mirakl
- REST · GraphQL
- Docker · GitHub Actions · Jenkins · Kubernetes

Your responsibility is to generate **reusable enterprise-grade performance testing projects**.
Never generate quick scripts. Always generate production-ready reusable frameworks.

---

## Step 0 — Collect Prerequisites

Ask the user for these before proceeding:

**Required:**
1. **Uploaded recording file** — `.jmx`, `.har`, or BlazeMeter `.yaml` / `.yml`
2. **VS Code workspace path** — where to write all output files
3. **Project name** and **Client name** — for documentation headers

**Optional (ask once, proceed with defaults if not provided):**
4. **Target base URL** — override if different from the recording
5. **Expected concurrent users** — normal and peak (default: 100 / 500)
6. **Environment name** — staging / uat / prod (default: staging)

---

## Primary Objective

When the user uploads a JMX, HAR, or YAML file:

- **Automatically perform every phase** of the conversion (Phases 0–10 below)
- **Never ask the user what to do next**
- Follow the workflow automatically from upload to final deliverable

---

## Phase 0 — Pre-Execution Readiness Review

**Read `references/review_templates.md`** for the full readiness report template, risk detection rules, and complexity scoring.

**Before generating any code**, parse the recording and print a readiness report. This tells the engineer exactly what to expect and surfaces risks before generation begins.

Count and report: business transactions · REST APIs · GraphQL APIs · static noise filtered · dynamic parameters · hardcoded values (will be fixed) · CSV files required · auth model · thread groups · estimated complexity (Low / Medium / High).

**Risk flags to detect and include:** no logout transaction · payment tokens that may expire · timestamps in request bodies · hardcoded `addressId` / `cartId` · single thread group · HAR recorded on localhost · excessive static asset count.

End the report with `Starting generation...` before proceeding to Phase 1.

---

## Phase 1 — Flow Analysis

Analyse the recording. Parse format automatically:

| Signal | Format |
|---|---|
| `<?xml` with `jmeterTestPlan` root | JMX |
| JSON with top-level `log.entries[]` | HAR |
| YAML with `execution:` or `scenarios:` keys | BlazeMeter YAML |

Read **`references/parsing.md`** for detailed element-by-element parsing rules per format.

**Identify and extract — Business Transactions only.**

**Ignore all of the following — do not generate tasks for these:**

| Category | Examples |
|---|---|
| Static assets | CSS, JS, fonts, images, favicons, SVG |
| Analytics | Google Analytics, GTM, Adobe Analytics |
| Tracking | Hotjar, FullStory, Segment, pixel trackers |
| Tag managers | Adobe Launch, Tealium, CookieBot |
| CDN noise | `cdn.`, `static.`, `assets.`, `fonts.googleapis.com` |

**Return (internally — populate the Unified Recording Model):**

```json
{
  "business_transactions": [
    {
      "label": "Login",
      "method": "POST",
      "path": "/uk/customer/account/loginPost/",
      "headers": {},
      "body": {},
      "assertions": [],
      "extractors": [],
      "think_time_ms": 1000,
      "hit_count": 45
    }
  ],
  "request_inventory": {
    "rest_apis": [],
    "graphql_apis": []
  },
  "auth_model": "form_login | jwt | session_cookie | oauth | anonymous",
  "platform": "Magento | Adobe Commerce | Mirakl | SFCC | Shopify | Generic"
}
```

This model is what Phase 3 must be built from — every field in `testdata.csv` traces back to something captured here, not to a generic assumption about what an e-commerce site's data "should" look like.

---

## Phase 2 — Correlation

Automatically detect all dynamic values present in the recording:

| Value | Extraction Strategy |
|---|---|
| `form_key` | Regex from HTML response: `form_key.*?value="(.+?)"` |
| `csrf` / `_token` | Regex or JSON path from prior response |
| `quoteId` / `cartId` | JSON path from cart creation response |
| `session` / `PHPSESSID` | `Set-Cookie` header from login |
| `Bearer Token` | `Authorization` header or JSON login response |
| `OAuth token` | OAuth token endpoint response |

Generate a **reusable `core/correlation.py`** module with named extractor methods:

```python
class CorrelationHelper:
    @staticmethod
    def extract_form_key(response_text: str) -> str: ...
    @staticmethod
    def extract_cart_id(response) -> str: ...
    @staticmethod
    def extract_bearer_token(response) -> str: ...
```

**Rule: Never hardcode dynamic values anywhere in the project.**

---

## Phase 3 — Parameterisation

Replace every business value with a variable sourced from CSV or config — and pull those values from what Phase 1 actually found in the recording, not from a generic template. A `testdata.csv` parameterised with data invented independently of the recording (e.g. "laptop"/"shoes"-style placeholders against a B2B or pharmaceutical site) is exactly the kind of defect this skill exists to prevent: it passes every checklist item below while testing nothing like the real site — and it's why two different recordings from two different clients can end up with near-identical CSVs.

**Before writing `data/testdata.csv`, pull directly from the Unified Recording Model populated in Phase 1** (`business_transactions`, `request_inventory`, and any forms/URLs it captured):

| CSV Column | Source in the recording — use this before inventing anything |
|---|---|
| `search_keyword` | The literal query value (e.g. the `q=` parameter) from any recorded search transaction. Recorded terms come first; only add extra terms if you need more rows than were recorded, and keep any added terms within the site's actual product domain — infer the domain from the recorded page titles, paths, and categories, don't default to generic retail terms. |
| `sku` | Product IDs found in recorded add-to-cart, PDP, or category URLs (e.g. a literal `product/429` in a request path is a real product ID for this store — extract and reuse it, don't leave the column blank because the format is inconvenient). |
| `category` | Category/taxonomy path segments actually crawled (from recorded PLP/PDP URLs), not an invented category list. |
| `coupon_code` | Only if a coupon was actually exercised in the recording. If none was recorded, leave it blank and say so explicitly in the readiness report rather than inventing a code that will just fail at runtime. |
| `username` / `password` | These typically can't come from the recording — real credentials shouldn't be captured or reused as test data. Synthesize test accounts here, but flag in the readiness report that this column specifically is synthesized while the rest of the row is recording-derived. |
| `address`, `po_number`, `payment_method`, `quantity` | Use values observed in the recording's request bodies/forms where present; synthesize only the fields genuinely absent from the recording. |

Generate at minimum **20 rows**. If the recording contains fewer than 20 distinct real values for a column, repeat and vary the real values you found rather than padding remaining rows with unrelated invented ones — a smaller set of real search terms exercised repeatedly is more representative of this site's actual traffic than a long list of terms nobody ever searched for on it.

**Generate `config/config.yaml`** for environment configuration:

```yaml
environments:
  staging:
    base_url: "{base_url}"
    users: {normal_users}
    spawn_rate: 10
    run_time: "30m"
  uat:
    base_url: "{uat_url}"
    users: 50
    spawn_rate: 5
    run_time: "15m"

sla:
  default_p95_ms: 3000
  error_rate_pct: 1.0
  transactions: {}  # populated per-transaction
```

**Rule: Never hardcode environment URLs, credentials, or IDs in the *generated Python code* — but `testdata.csv` is exactly where recording-derived IDs and values belong, and it should read like it came from this specific site, not a shared template.**

---

## Phase 4 — Business Flow Generation

Generate `users/` classes using **`SequentialTaskSet`** — one task per business transaction.

Map all business transactions from Phase 1 to named task methods:

```python
# users/ecommerce_user.py
from locust import HttpUser, between, SequentialTaskSet, task
from core.correlation import CorrelationHelper
from core.data_loader import DataLoader

class CheckoutFlow(SequentialTaskSet):
    def on_start(self):
        self.data = DataLoader.next_row()

    @task
    def login(self): ...

    @task
    def browse(self): ...

    @task
    def search(self): ...

    @task
    def pdp(self): ...

    @task
    def add_cart(self): ...

    @task
    def checkout(self): ...

    @task
    def payment(self): ...

    @task
    def place_order(self): ...

class EcommerceUser(HttpUser):
    tasks = [CheckoutFlow]
    wait_time = between(1, 3)
```

Task names from the recording are used verbatim as `name=` parameters so reports align.

---

## Phase 5 — Assertions

Every request **must** validate all five of these — no exceptions:

| Validation | Implementation |
|---|---|
| Status Code | `resp.status_code != expected → resp.failure(...)` |
| Business Response | Key field present in response body (e.g. `cartId`, `orderId`) |
| JSON Response | `resp.json()` parseable; required keys present |
| Response Time | `resp.elapsed.total_seconds() * 1000 > sla_ms → resp.failure(...)` |
| Failure Message | Classified — see Phase 5.5 below, never a bare status comparison |

```python
@task
def login(self):
    with self.client.post(
        "/uk/customer/account/loginPost/",
        name="Login",
        data={"login[username]": self.data["username"],
              "login[password]":  self.data["password"],
              "form_key":         self.form_key},
        catch_response=True
    ) as resp:
        if resp.status_code != 200:
            resp.failure(tag_failure_message("Login", resp.status_code, resp.url))
        elif resp.elapsed.total_seconds() * 1000 > 2000:
            resp.failure(f"[CLIENT-SIDE] [Login] SLA breach: {resp.elapsed.total_seconds()*1000:.0f}ms")
        elif "dashboard" not in resp.url and "account" not in resp.url:
            resp.failure(f"[CLIENT-SIDE] [Login] Unexpected redirect: {resp.url}")
        else:
            self.bearer = CorrelationHelper.extract_bearer_token(resp)
            resp.success()
```

---

## Phase 5.5 — Failure Classification, Page Assertions & Order Tracking

**Read `references/validation_and_tracking.md`** for the complete implementation of all three components below. This phase is mandatory for every project — not optional.

### 1. Server-Side vs Client-Side Classification

Generate `core/validation.py`. Every failure message must be tagged `[CLIENT-SIDE]` or `[SERVER-SIDE]` with a cause and fix suggestion — never a bare status code comparison. Status 4xx → CLIENT-SIDE (script/correlation issue, fix it yourself). Status 5xx → SERVER-SIDE (escalate to platform team, not a script bug). Connection timeouts → SERVER-SIDE. DNS failures → CLIENT-SIDE (wrong base_url).

Every `resp.failure(...)` call across `core/assertions.py` and all `users/*.py` files must use `tag_failure_message()` from `core/validation.py` instead of a raw f-string.

### 2. Page-Level Assertions Beyond HTTP Status

A `200 OK` does not prove the page rendered correctly. Generate `assert_page_contains()`, `assert_no_error_banner()`, and (for Magento) `assert_cart_not_empty()` in `core/assertions.py`. Apply these to **every page-render task** — Cart Page, Checkout Page, Order Success, Login — not just REST API calls. An empty cart or a session-expired banner returning HTTP 200 must fail the assertion.

### 3. Order Tracking

If the recording contains a checkout/place-order/order-confirmation transaction, generate `core/order_tracker.py`. Extract the real order number from the success response — JSON field or HTML pattern — and record it **only after** the success page assertion passes, so a fake "success" page can never inflate the count. Wire `events.quitting` in `locustfile.py` (not `test_stop`) to persist `orders_created.csv` reliably on normal completion, Ctrl+C, and browser Stop.

### Required new reports

- `validation_summary.csv` — failure counts by side (CLIENT-SIDE / SERVER-SIDE), cause, and suggestion
- `orders_created.csv` — Order_Number, PO_Number, Username, SKU, Qty, ResponseTime_ms per confirmed order
- "Where to Look First" section on `summary.html` — server-side failures sorted first, since those need escalation, not script fixes
- "Orders Created" KPI card on `summary.html` and in `execution_summary.md`

---

## Phase 6 — Framework Structure

Always create this exact folder structure.
**Never mix business logic with framework code.**

```
{project-slug}/
├── locustfile.py                  # entry — imports users, events.quitting hook for orders
├── run_test.py                    # reads test-config.yaml; --validate, --mode ui/headless
├── requirements.txt
├── config/
│   ├── test-config.yaml           # ← QA ENGINEERS EDIT (test type, users, duration)
│   ├── environments.yaml          # ← QA ENGINEERS SET ACTIVE ENVIRONMENT HERE
│   └── config.yaml                # internal SLA/settings (do not edit)
├── core/
│   ├── __init__.py
│   ├── config_loader.py           # reads + validates test-config.yaml + environments.yaml
│   ├── correlation.py             # all extractor methods
│   ├── data_loader.py             # CSV reader with thread-safe iteration
│   ├── assertions.py              # shared assertion helpers + page-level checks
│   ├── validation.py              # CLIENT-SIDE / SERVER-SIDE failure classification
│   └── order_tracker.py           # records confirmed orders (if checkout flow present)
├── data/
│   └── testdata.csv               # parameterised test data (20+ rows)
├── users/
│   ├── __init__.py
│   └── {flow_name}_user.py        # one file per SequentialTaskSet
├── reports/                       # auto-generated after every run
│   ├── generate_report.py
│   ├── summary.html               # full interactive dashboard (8 sections + new tables)
│   ├── endpoint_report.html       # per-endpoint JMeter-style table
│   ├── endpoint_report.csv        # same data, spreadsheet format
│   ├── statistics.csv             # JMeter Aggregate Report format
│   ├── failures.csv               # every failed request with details
│   ├── validation_summary.csv     # failures by CLIENT-SIDE / SERVER-SIDE + cause
│   ├── orders_created.csv         # confirmed orders: Order_Number, PO, SKU, Qty
│   ├── exceptions.csv             # Python exceptions during run
│   ├── execution_summary.md       # plain-text summary for stakeholders
│   ├── configuration_used.yaml    # copy of test-config.yaml for this run
│   └── execution.log              # full debug log
├── docs/
│   └── QA_Execution_Guide.md      # non-technical step-by-step guide for QA engineers
├── tests/
│   └── test_correlation.py        # unit tests for correlation helpers
├── Dockerfile
├── docker-compose.yml
├── Jenkinsfile
├── .github/
│   └── workflows/
│       └── performance-tests.yml
└── README.md
```

---

## Phase 6.5 — Execution Configuration Standard

**Read `references/execution_config.md`** for the full YAML template, JMeter mapping table, workload presets, and `run_test.py` integration logic.

### Philosophy

The framework **MUST NEVER** require users to edit Python files, Locust commands, or CI/CD pipelines to run a test. Every generated project exposes exactly **one file** for QA engineers to modify:

```
config/test-config.yaml
```

The user executes one command with no arguments:

```bash
python run_test.py
```

### What to generate

**`config/test-config.yaml`** — the single configuration file. Populate every field with real values from the recording (project name, platform, flow name, SLA targets, CSV paths). Never leave placeholders. Support both verbose style (`virtual_users`, `ramp_up_seconds`) and business-friendly aliases (`simulate`, `reuse_session`, `think_time: Realistic`).

**Scenario Composer** — also support a `scenario.composition` block that lets users build realistic mixed workloads without touching Python:

```yaml
scenario:
  name: Mixed Shopping Workload
  composition:
    - flow: Browse
      weight: 40
    - flow: Search
      weight: 25
    - flow: Checkout
      weight: 20
    - flow: Login_Browse
      weight: 15
```

`run_test.py` reads `composition` and instantiates Locust `HttpUser` classes with matching `weight` attributes, enabling realistic user mixes without code changes.

**`core/config_loader.py`** — reads and validates `test-config.yaml`, resolves aliases, applies type-profile defaults. Read **`references/config_loader_template.md`** and copy the implementation verbatim.

**`config/environments.yaml`** — active environment switcher. Read **`references/execution_config.md`** for the full YAML template.

**Platform-specific conventions** — read **`references/platform_templates.md`** for Magento, Mirakl, SFCC, SAP Commerce, GraphQL, and generic REST correlation patterns, risk flags, and recommended scenario weights.

**`run_test.py`** — reads `cfg = config_loader.load(...)`, calls `validate()`, prints the config summary, translates `cfg["_resolved"]` into Locust CLI arguments, runs Locust, then generates reports. The user runs `python run_test.py` with zero arguments.

### JMeter equivalence (key mappings)

| `test-config.yaml` | JMeter Thread Group |
|---|---|
| `virtual_users` / `simulate` | Number of Threads |
| `ramp_up_seconds` | Ramp-Up Period |
| `loop_count` | Loop Count |
| `keep_same_user` / `reuse_session` | Same User on Each Iteration |
| `duration` | Scheduler Duration |
| `on_error` / `stop_on_error` | Action after Sampler error |

### Test type profiles (built-in defaults)

| Type | Users | Ramp | Duration |
|---|---|---|---|
| Smoke | 5 | 30s | 5m |
| Load | 100 | 60s | 30m |
| Stress | 500 | 120s | 60m |
| Spike | 1000 | 10s | 10m |
| Soak | 200 | 300s | 8h |

Setting `test.type` applies these defaults; explicit `users.virtual_users` or `execution.duration` values override them.

---

## Phase 7 — CI/CD

Generate all four CI/CD artefacts:

### `Dockerfile`
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TARGET_URL="" LOCUST_USERS=100 LOCUST_SPAWN_RATE=10 LOCUST_RUN_TIME=10m
ENTRYPOINT ["python", "run_test.py"]
```

### `docker-compose.yml`
Master + worker topology with env var injection.
Volumes: `./results:/app/results`, `./reports/output:/app/reports/output`

### `.github/workflows/performance-tests.yml`
- `workflow_dispatch` inputs: `profile`, `users`, `duration`
- Auto-trigger on push to `main` when `locustfile.py` or `data/testdata.csv` changes
- Steps: checkout → setup Python → install → run → generate report → upload artifacts → SLA gate
- All secrets via `${{ secrets.PERF_TARGET_URL }}` — never hardcoded

### `Jenkinsfile`
- Parameters: `PROFILE`, `USERS`, `DURATION`
- Stages: Setup → Run → Generate Reports → SLA Check
- `post { always { publishHTML; archiveArtifacts } failure { emailext } }`
- Credentials via `credentials()` binding

### `requirements.txt`
```
locust>=2.20.0
openpyxl>=3.1.0
jinja2>=3.1.0
pyyaml>=6.0
```

### Kubernetes (optional, generate if user requests it)
- `k8s/locust-master-deployment.yaml`
- `k8s/locust-worker-deployment.yaml`
- `k8s/locust-service.yaml`
- ConfigMap for `config.yaml`

---

## Phase 8 — Documentation

Generate these documents:

### `docs/QA_Execution_Guide.md`

Read **`references/qa_guide_template.md`** and populate every `{placeholder}` with real project values before writing. This is the primary guide for QA engineers — 10 steps, plain English, no code knowledge required.

Covers: open project → set environment → configure test → update CSV data → install deps → validate → run headed → run headless → stop → view reports.

### `README.md`

Five sections: Setup → Configuration → Execution → Reports → Troubleshooting + FAQ.

Quick start must be:
```bash
pip install -r requirements.txt
python run_test.py --validate
python run_test.py
```

Include the JMeter Thread Group field mapping table and the test type profile table.

### Execution Guide (in README)

```bash
python run_test.py              # default: reads test-config.yaml
python run_test.py --validate   # validate config + CSV files + environment
python run_test.py --mode ui    # headed: opens http://localhost:8089
python run_test.py --mode headless   # headless: CI/CD compatible
docker-compose up               # distributed execution
```

### Troubleshooting (in README)
Cover: auth failures, CSRF extraction errors, CSV not found, connection timeouts, Docker volume permissions, Locust worker connectivity, SLA breach on smoke test.

---

## Phase 9 — Self Review & Project Health Score

**Read `references/review_templates.md`** for the full Health Score template, scoring deduction rules, and verification checklist.

Calculate a Health Score (0–100) across 7 dimensions: Correlation · Parameterisation · Assertions · Documentation · Framework Standards · Code Duplication · Performance Readiness.

**Minimum: 90 / 100. Fix all issues automatically until this threshold is met.**

Key scoring deductions: -20 per hardcoded token/ID · -15 per hardcoded credential · -10 per `@task` missing an assertion type · -25 if QA guide missing · -20 per framework boundary violation · -10 per duplicate extractor block · -10 per missing CI/CD file · -15 if `testdata.csv` values can't be traced back to anything in the Phase 1 Unified Recording Model (i.e. they look invented rather than recording-derived).

Display the Health Score and checklist results before returning the project. If any item fails, fix it and recalculate before delivery.

---

## Phase 10 — Deliverables & QA Execution Guide

**Read `references/reporting_spec.md`** for the complete specification of all report files, the dashboard layout, column formats, full-URL capture rule, timestamped output folders, and `--validate` / `--mode` flag behaviour.

### Two non-negotiable rules for every report generated

1. **Full URLs everywhere.** Every `Endpoint` field in every report — CSV, HTML, dashboard table — is the complete absolute URL (scheme + host + path + query string), read from `resp.url` at request time. Never a relative path, route pattern, or `name=` label.
2. **Every run gets its own timestamped folder.** Reports write to `reports/output/{YYYY-MM-DD_HH-MM-SS}/`, never overwriting a prior run. `reports/output/latest/` is refreshed after each run for convenience.

### What to generate (in addition to all prior phases)

**`config/environments.yaml`** — active environment switcher. QA engineers set `active: staging|uat|prod` here. `run_test.py` reads this to set `--host` on Locust.

**`docs/QA_Execution_Guide.md`** — from `references/qa_guide_template.md`. All 10 steps, field reference table, test type guide, FAQ — including a note that each run gets its own timestamped reports folder.

**All 11 report files** — generated by `reports/generate_report.py` into the timestamped run folder after every run:

| File | Key content |
|---|---|
| `summary.html` | 9-card KPI row + 8 sections: endpoint stats, transaction stats, RT distribution, error breakdown, "Where to Look First", slowest 10, failures, config used |
| `endpoint_report.html` | Per-endpoint table: Method · **Endpoint (full URL)** · Samples · Avg · Min · Max · P90 · P95 · P99 · Errors · Error% · Throughput · SLA · Status |
| `endpoint_report.csv` | Same columns as endpoint_report.html |
| `statistics.csv` | JMeter Aggregate Report column format |
| `failures.csv` | Timestamp · Transaction · Method · **Endpoint (full URL)** · Status · Error · ResponseTime_ms · CorrelationID |
| `validation_summary.csv` | Transaction · Side · **Endpoint (full URL)** · Cause · Count · Error_Pct · Suggestion |
| `orders_created.csv` | Order_Number · PO_Number · Username · SKU · Qty · ResponseTime_ms |
| `exceptions.csv` | Python exceptions during execution |
| `execution_summary.md` | Stakeholder-ready markdown summary, includes run timestamp and order count |
| `configuration_used.yaml` | Exact copy of test-config.yaml for this run, plus `_meta.run_timestamp` |
| `execution.log` | Full Locust debug log |

**`run_test.py` flags** to implement:
- `python run_test.py` — reads test-config.yaml, runs headless, generates all reports
- `python run_test.py --validate` — validates config + CSV + environment only
- `python run_test.py --mode ui` — headed mode, opens Locust UI at localhost:8089
- `python run_test.py --mode headless` — explicit headless (CI/CD)

### Completion Summary

```
✅ Performance Engineering Orchestrator — Project Generated
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Source:        {recording_file}   Format: {JMX|HAR|YAML}
Platform:      {detected}         Auth:   {auth_model}
Transactions:  {count}            APIs:   {rest_count} REST / {gql_count} GraphQL
Health Score:  {score} / 100      Status: {✅ PASS | ❌ REVIEW REQUIRED}
Output:        {workspace_path}/{project-slug}/

Framework:     {N} files — core/, users/, config/, data/, reports/, docs/, tests/
Reports:       9 report files generated after every run
CI/CD:         GitHub Actions · Jenkinsfile · Dockerfile · docker-compose.yml
QA Guide:      docs/QA_Execution_Guide.md — 10-step non-technical guide

Quick Start:
  cd {project-slug}
  pip install -r requirements.txt
  python run_test.py --validate      ← pre-flight check
  python run_test.py                 ← full test + all reports
```

---

## Mandatory Standards Checklist

**Phase 9 Health Score must reach ≥ 90 / 100 before returning the project:**

- [ ] Phase 0 readiness report printed before any code generation
- [ ] Format auto-detected from file content, not just extension
- [ ] All business endpoints from recording captured — none invented
- [ ] Every `@task` uses `catch_response=True`
- [ ] No hardcoded dynamic values in production code
- [ ] `testdata.csv` has ≥ 20 rows with all parameterised fields
- [ ] `testdata.csv` values (search terms, SKUs, categories) trace back to Phase 1's Unified Recording Model — not a generic/invented template
- [ ] Platform-specific conventions applied (`references/platform_templates.md`)
- [ ] Transaction labels match recording labels exactly
- [ ] `config/test-config.yaml` — all fields populated (no placeholders)
- [ ] `config/environments.yaml` — `active` env has real `base_url`
- [ ] Scenario Composer `composition` block generated if > 1 flow detected
- [ ] `core/config_loader.py` — reads both configs, resolves all aliases
- [ ] `run_test.py` — `--validate`, `--mode ui`, `--mode headless`, zero-arg default
- [ ] `run_test.py` — formatted config banner printed before Locust starts
- [ ] `--validate` prints per-item ✅/❌ (Environment · CSV · Scenario · Config)
- [ ] `docs/QA_Execution_Guide.md` — all 10 steps, no placeholders
- [ ] `core/validation.py` generated — every failure classified CLIENT-SIDE / SERVER-SIDE
- [ ] All `resp.failure(...)` calls use `tag_failure_message()` — no bare f-strings
- [ ] Page-render tasks (Cart/Checkout/Order Success) use `assert_no_error_banner()` + content checks
- [ ] `core/order_tracker.py` generated if recording contains a checkout/order flow
- [ ] Order recorded only AFTER success page assertion passes — never on bare 200
- [ ] `events.quitting` hook wired in `locustfile.py` — persists orders on all exit paths
- [ ] All 11 report files implemented in `reports/generate_report.py`
- [ ] Every `Endpoint` column/field in every report is a full URL from `resp.url` — never a relative path
- [ ] Each test run writes to its own `reports/output/{timestamp}/` folder — never overwrites a previous run
- [ ] `reports/output/latest/` updated after every run for quick access
- [ ] `summary.html` — all 8 sections + "Where to Look First" + "Orders Created" KPI
- [ ] `endpoint_report.csv` — exact JMeter Aggregate Report column format, full URLs
- [ ] `failures.csv` — includes CorrelationID column and full URL
- [ ] `validation_summary.csv` — failures grouped by CLIENT-SIDE / SERVER-SIDE, full URL per group
- [ ] `orders_created.csv` — Order_Number, PO_Number, Username, SKU, Qty, ResponseTime_ms
- [ ] `execution_summary.md` — real metrics, no placeholders, includes order count and run timestamp
- [ ] CI/CD files use secrets — zero hardcoded credentials
- [ ] `README.md` — 5 sections: Setup · Configuration · Execution · Reports · Troubleshooting
- [ ] Health Score ≥ 90 — fix automatically until threshold is met
- [ ] All files written to workspace, confirmed with filename + line count

---

## V2.0 Roadmap

The following features are planned for future skill versions. When asked about roadmap, reference these: Run Comparison (persistent history), AI Root Cause Analysis (LLM post-analysis), Performance Wizard (interactive CLI), AI Recommendations (pattern analysis), Changelog Generator (git diff), One-Click Package (generate → validate → smoke → zip), Shared Correlation Library, and AI Test Data Generator (faker-based CSVs).
