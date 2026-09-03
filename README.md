# LT Metrics — Load Testing Reimagined

Point LT Metrics at **any URL** and it will crawl the target, model a realistic
workload, generate a production-ready Locust script, run the load test, and
produce JMeter-style reports with AI recommendations — all from a dead-simple
web UI, with **zero performance-engineering knowledge required.**

## What it does

LT Metrics orchestrates six specialized agents behind one screen:

| Agent | Responsibility |
|---|---|
| **Discovery** | Crawls any URL, detects tech stack & business domain, maps user journeys |
| **Planner** | Turns "I expect 5,000 visitors/hour" into concurrent users, ramp, duration, pacing & exit criteria |
| **Generator** | Emits a grounded Locust script — CSV-driven, CSRF-correlated, validated, weighted |
| **Reviewer** | Audits the script against an enterprise QA checklist |
| **Executor** | Runs Locust headless and streams live stats |
| **RCA / Reporter** | JMeter-style metrics, SLA gate, recommendations, root-cause, trend vs baseline, HTML + Excel |

Supports all eight test types: **Smoke, Baseline, Load, Stress, Spike, Soak,
Capacity, Volume.** Every run is stored in a SQLite ledger for historical
trend comparison and natural-language queries.

## Quick start

```bash
# 1. install (Python 3.9+)
pip install -r requirements.txt

# 2. verify everything works (offline self-test)
python selftest.py

# 3. launch the app
python run.py
# opens http://127.0.0.1:8000
```

Then in the browser:

1. **New Test** → enter a Project name + Target URL → **Discover**
2. Pick a test type in plain English, set expected/peak users → **Preview plan**
3. **Generate & Run Test** → watch live progress → open the full report

## Headless CLI (for automation & CI)

Run the entire pipeline without the UI and gate on the SLA result:

```bash
python -m ltmetrics.cli --url https://example.com --test-type load \
    --expected-users 100 --check-sla
# exit code 0 = SLA passed, 1 = SLA breached, 2 = error
```

Flags: `--test-type` (smoke|baseline|load|stress|spike|soak|capacity|volume),
`--users`, `--duration` (seconds), `--spawn-rate`, `--workers`, `--username`,
`--password`, `--check-sla`. Each flag also has an `LTM_*` environment-variable
fallback (e.g. `LTM_TARGET_URL`, `LTM_USERS`) for CI.

## Distributed / multi-worker execution

Add `--workers N` (CLI) or the **Distributed workers** field (UI → Advanced) to
run a Locust **master + N worker** processes and generate more load from one
host:

```bash
python -m ltmetrics.cli --url https://example.com --test-type stress \
    --users 500 --workers 4 --check-sla
```

## Docker

```bash
docker build -t ltmetrics .
docker run -p 8000:8000 -v "$PWD/projects:/app/projects" ltmetrics      # web UI
# or headless in CI:
docker run --rm -v "$PWD/projects:/app/projects" ltmetrics \
    python -m ltmetrics.cli --url https://example.com --check-sla
```

Or with compose: `docker compose up ltmetrics` (UI) /
`docker compose run --rm runner --url https://example.com --check-sla`.

## CI/CD

- **GitHub Actions** — `.github/workflows/performance-tests.yml`: `workflow_dispatch`
  with inputs (url, test type, users, duration, workers) plus a `push` trigger.
  Uploads the HTML/Excel reports as artifacts and fails the job on SLA breach.
  Manual runs use the dispatch inputs; **push-triggered runs are configured
  before you push** via repo *Variables* (`PERF_TARGET_URL`, `PERF_TEST_TYPE`,
  `PERF_USERS`, `PERF_DURATION`, `PERF_WORKERS`) and *Secrets* (`PERF_USERNAME`,
  `PERF_PASSWORD`) under Settings → Secrets and variables → Actions.
- **Jenkins** — `ci/Jenkinsfile`: parameterized declarative pipeline with
  `publishHTML` + `archiveArtifacts`, gated on the SLA result.

## Project layout

```
Performance Orchestration/
├─ run.py                 # launcher (uvicorn)
├─ selftest.py            # offline end-to-end validation
├─ requirements.txt
├─ Dockerfile             # container image (web UI or CLI)
├─ docker-compose.yml     # web + headless runner services
├─ .github/workflows/     # GitHub Actions performance pipeline
├─ ci/Jenkinsfile         # Jenkins declarative pipeline
├─ ltmetrics/
│  ├─ server.py           # FastAPI orchestrator + REST API
│  ├─ cli.py              # headless CLI runner (CI entry point)
│  ├─ config.py           # paths, defaults, project isolation
│  ├─ db.py               # SQLite history ledger
│  ├─ reporting.py        # HTML dashboard + Excel workbook
│  ├─ static/index.html   # the web UI
│  └─ agents/
│     ├─ discovery.py     # crawler + domain detection
│     ├─ planner.py       # workload modeling
│     ├─ generator.py     # Locust script generation
│     ├─ reviewer.py      # QA audit
│     ├─ executor.py      # headless + distributed run, live monitoring
│     └─ analyzer.py      # RCA, SLA gate, recommendations, trends
├─ projects/              # per-run artifacts (auto-created)
│  └─ <project>/<url-slug>/<run-id>/{scripts,data,results,reports}
└─ ltmetrics_history.db        # SQLite ledger (auto-created)
```

## Notes

- **Only test systems you own or are authorized to load test.** Generating load
  against third-party sites without permission may violate their terms.
- Discovery uses HTTP + HTML parsing. On heavily JS-rendered sites it captures
  fewer endpoints but still crafts a valid browse workload; you can extend the
  generated `locustfile.py` in any run's `scripts/` folder.
- Reports for each run live in `projects/.../<run-id>/reports/`
  (`performance_report.html` and `performance_report.xlsx`).
