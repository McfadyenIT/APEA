# APEA — Step-by-Step Guide to Running a Performance Test

This guide assumes **no prior knowledge**. Follow it top to bottom.

APEA has two ways to run a test:

- **Headed mode** — a point-and-click website (the "UI"). Best for people who
  want a simple, visual experience. *Start here if you're new.*
- **Headless mode** — a single command in a terminal. Best for automation,
  scheduled runs, and CI/CD pipelines.

Both do exactly the same thing under the hood; only the way you start them differs.

> ⚠️ **Only test websites you own or are allowed to load-test.** Sending heavy
> traffic to a site you don't control can cause problems and may be against
> their rules.

---

## Part 0 — One-time setup (do this once)

1. **Install Python** (version 3.9 or newer), if you don't already have it.
   - Go to https://www.python.org/downloads/
   - Download and run the installer.
   - **On Windows, tick the box "Add Python to PATH"** during install. This is important.

2. **Get the APEA folder** onto your computer (you already have it at
   `Performance Orchestration`).

That's it for setup. The launcher handles the rest automatically.

---

## Part A — Headed mode (the website / UI)

### Step A1 — Start APEA

**Windows:** open the `Performance Orchestration` folder and **double-click
`start.bat`**.

**Mac/Linux:** open a Terminal, then type (adjust the path to where the folder is):

```
cd "Performance Orchestration"
./start.sh
```

The first time, it will take a minute to install everything. When it's ready you'll
see a message like `Open: http://127.0.0.1:8000` and your web browser will open
automatically.

> If the browser doesn't open, open it yourself and go to **http://127.0.0.1:8000**

Keep the black window (terminal) open — that's the engine. Closing it stops APEA.

### Step A2 — Fill in the basics

On the **New Test** tab:

1. **Project name** — any label you like, e.g. `My Store`.
2. **Target URL** — the website address to test, e.g. `https://www.example.com`.

### Step A3 — (Optional) Add your own data

Still on the New Test tab, you can upload files. **All of these are optional** —
skip them and APEA uses sensible defaults.

- **User credentials CSV** — a spreadsheet with columns like `username` and
  `password`. Upload this if the test needs to log in.
- **Product / search data CSV** — a spreadsheet with columns like `product_id`
  and `search_keyword`. APEA will use these in the search and product tests.
- **Recorded script** — a BlazeMeter/JMeter `.jmx`, a `.yaml`/`.yml`, or a
  browser `.har` recording. APEA reads the recorded steps and turns them into the test.

After you pick each file you'll see a green ✅ with how many rows/endpoints were found.

> **How to make a CSV:** open Excel or Google Sheets, put the column names in the
> first row (e.g. `username,password`), fill the rows, then **Save As → CSV**.

### Step A4 — Discover the site

Click **🔍 Discover**. APEA visits the site, figures out what kind of app it is
(shop, blog, etc.), and lists the user journeys it will simulate. This takes a few
seconds.

### Step A5 — Choose the test (in plain English)

In section **2 · Choose a test**:

1. **Test type** — pick one. If unsure, start with **Smoke Test** (tiny, ~2 min,
   just proves everything works). Other options: Load (normal traffic), Stress
   (find the breaking point), Spike, Soak, etc.
2. **Expected users** and **Peak users** — roughly how many people use the site at
   once. Defaults (100 / 500) are fine to start.
3. Click **Preview plan** to see what APEA will do (users, duration, pass/fail
   limits).

*(Optional)* Under **Advanced overrides** you can force a specific number of users,
duration, or **Distributed workers** (run several load generators at once for
heavier tests).

### Step A6 — Run it

Click **▶ Generate & Run Test**. You'll see:

1. **Script review** — a checklist confirming the generated test is valid.
2. **Live execution** — a progress bar with live users, requests/sec, and response
   times. Wait for it to finish (a Smoke test is ~2 minutes).

### Step A7 — Read the results

When it finishes you get:

- A **PASS ✅ / FAIL ❌** quality gate (did it meet the response-time and error
  limits).
- Key numbers (requests, errors, average and 95th-percentile response time).
- **AI recommendations** — prioritized suggestions.
- Click **📊 Open full report** for the detailed dashboard, or **⬇ Download Excel**
  for a spreadsheet.

### Step A8 — Look back later

- **History** tab — every past run, with links to reopen the report.
- **Projects** tab — reuse a saved project (click *use* to reload its settings).
- **Ask APEA** tab — type questions like
  *"show checkout regressions over the last 6 months"*.

### Step A9 — Stop APEA

Close the black terminal window, or press **Ctrl + C** in it.

---

## Part B — Headless mode (one command, no UI)

Use this for automation, scheduled runs, or CI/CD. It does the whole test and
prints a summary; it also returns a pass/fail code so pipelines can gate on it.

### Step B1 — Open a terminal in the project folder

**Windows:** open the `Performance Orchestration` folder, click the address bar,
type `cmd`, and press Enter. Then turn on the environment:

```
.venv\Scripts\activate
```

**Mac/Linux:**

```
cd "Performance Orchestration"
source .venv/bin/activate
```

> If you've never run `start.bat`/`start.sh` before, first run:
> `python -m venv .venv` then activate it (as above) then
> `pip install -r requirements.txt`.

### Step B2 — Run a test

The basic command:

```
python -m apea.cli --url https://www.example.com --test-type smoke
```

You'll see progress printed live, then a results summary and a line telling you
where the report was saved.

### Step B3 — Common options

Add any of these to the command:

| Option | What it does | Example |
|---|---|---|
| `--test-type` | smoke, baseline, load, stress, spike, soak, capacity, volume | `--test-type load` |
| `--expected-users` | normal number of users | `--expected-users 200` |
| `--users` | force an exact user count | `--users 500` |
| `--duration` | test length in **seconds** | `--duration 900` |
| `--workers` | run several load generators at once | `--workers 4` |
| `--username` / `--password` | log in during the test | `--username joe --password secret` |
| `--users-csv` | credentials CSV file | `--users-csv data\users.csv` |
| `--data-csv` | product_id / search_keyword CSV | `--data-csv data\products.csv` |
| `--recording` | a .jmx / .yaml / .har recording | `--recording recording.jmx` |
| `--check-sla` | **fail** (exit code 1) if limits are breached — use in CI | `--check-sla` |

**Full example** (a stress test with 4 workers, product data, gated for CI):

```
python -m apea.cli --url https://www.example.com --test-type stress ^
  --users 500 --duration 900 --workers 4 ^
  --data-csv data\products.csv --check-sla
```

*(On Mac/Linux use `\` at line ends instead of `^`, or put it all on one line.)*

### Step B4 — Understand the exit code

After the run, the command reports one of:

- **0** — test ran and passed the limits (or `--check-sla` wasn't used).
- **1** — test ran but **breached the SLA** (only when `--check-sla` is set).
- **2** — an error occurred (e.g., the site was unreachable, Locust not installed).

To see the code:
- Windows: `echo %ERRORLEVEL%`
- Mac/Linux: `echo $?`

### Step B5 — Find the report

The path is printed at the end, e.g.:

```
projects\<project>\<site>\<run-id>\reports\performance_report.html
```

Open that `.html` in a browser for the full dashboard; the `.xlsx` next to it is
the spreadsheet.

---

## Part C — Running in CI/CD (no uploading)

In CI/CD you never use the browser upload buttons. CI runs the **headless
command** (Part B). The key idea:

> **Commit your files to the repository once.** The pipeline checks them out on
> every run and reads them by path. There is no "upload" step in CI.

### Step C0 — Pick your mode

There are two ways to run in CI:

- **Crawl‑only (no files needed):** give just a URL. APEA crawls the site and
  runs a browse/search test. Good for a quick health gate on every push.
- **With a recording + data (recommended for real flows):** commit your
  BlazeMeter/JMeter recording and CSVs; APEA replays the full checkout/login
  flow. Use this when you need login validation and orders.

### Step C1 — Commit your test files (only if using the "with files" mode)

1. In your repository, create a folder for performance assets, e.g. **`ci/perf/`**.
2. Put your files there and commit them, for example:
   - `ci/perf/checkout.yaml`  (your BlazeMeter/Taurus recording, or `.jmx` / `.har`)
   - `ci/perf/data.csv`       (columns like `product_id, search_keyword`)
3. **Do NOT commit passwords.** Keep credentials out of git — pass them as CI
   **secrets** instead (see below). A `users.csv` without passwords is fine, but
   real credentials belong in secrets.

### Step C2 — GitHub Actions (detailed)

A ready workflow ships at `.github/workflows/performance-tests.yml`.

**One‑time configuration** in your repo → **Settings → Secrets and variables →
Actions**:

- On the **Variables** tab, add (these are safe, non‑secret values):

  | Variable | Example | Meaning |
  |---|---|---|
  | `PERF_TARGET_URL` | `https://mcstaging.amneal.com` | Site to test |
  | `PERF_TEST_TYPE` | `smoke` | Test profile for push runs |
  | `PERF_USERS` | `50` | (optional) force user count |
  | `PERF_DURATION` | `300` | (optional) seconds |
  | `PERF_WORKERS` | `1` | (optional) parallel workers |
  | `PERF_RECORDING` | `ci/perf/checkout.yaml` | (optional) committed recording path |
  | `PERF_USERS_CSV` | `ci/perf/users.csv` | (optional) committed credentials CSV path |
  | `PERF_DATA_CSV` | `ci/perf/data.csv` | (optional) committed data CSV path |

- On the **Secrets** tab, add credentials (kept private):

  | Secret | Meaning |
  |---|---|
  | `PERF_USERNAME` | login username/email |
  | `PERF_PASSWORD` | login password |

**Run it two ways:**

- **Automatically on every push** to `main` (uses the Variables/Secrets above).
- **On demand:** repo → **Actions → APEA Performance Tests → Run workflow**, then
  fill in the boxes (url, test type, users, duration, workers, and optionally the
  `recording` / `users_csv` / `data_csv` paths). Manual inputs override the
  Variables for that run.

**What the job does:** installs dependencies → runs
`python -m apea.cli --check-sla` → uploads `performance_report.html`/`.xlsx` as
downloadable **artifacts** → **fails the build if the SLA is breached** (so a bad
result blocks the pipeline). Leave the recording/CSV variables blank for a
crawl‑only run.

### Step C3 — Jenkins (detailed)

1. Install the **HTML Publisher** plugin.
2. (Optional, for login) create a **Username/Password credential** with ID
   `perf-creds`.
3. New Item → **Pipeline** → under *Pipeline*, choose **Pipeline script from
   SCM**, point it at your repo, and set the script path to **`ci/Jenkinsfile`**.
4. Click **Build with Parameters** and fill in: `TARGET_URL`, `TEST_TYPE`,
   `USERS`, `DURATION`, `WORKERS`, and (for the recorded flow) `RECORDING`,
   `USERS_CSV`, `DATA_CSV` — set these to the committed paths, e.g.
   `ci/perf/checkout.yaml`. Leave blank for crawl‑only.

The pipeline runs the headless test, **publishes the HTML report**, archives the
artifacts, and **fails the build on an SLA breach**.

### Step C4 — Any other CI (GitLab, Azure DevOps, etc.)

Three steps in any runner that has Python:

```yaml
- pip install -r requirements.txt
- python -m apea.cli --url "$APEA_TARGET_URL" --test-type smoke --check-sla
# then archive: projects/**/reports/performance_report.html and .xlsx
```

To use committed files, add the paths (or set the `APEA_*` env vars):

```
python -m apea.cli --url "$APEA_TARGET_URL" \
  --recording ci/perf/checkout.yaml \
  --users-csv ci/perf/users.csv \
  --data-csv  ci/perf/data.csv \
  --username "$APEA_USERNAME" --password "$APEA_PASSWORD" \
  --check-sla
```

**Environment‑variable equivalents** (so you can avoid flags entirely):
`APEA_TARGET_URL`, `APEA_TEST_TYPE`, `APEA_USERS`, `APEA_DURATION`,
`APEA_WORKERS`, `APEA_RECORDING`, `APEA_USERS_CSV`, `APEA_DATA_CSV`,
`APEA_USERNAME`, `APEA_PASSWORD`.

### Step C5 — Read the CI result

- **Exit code** gates the build: `0` = passed, `1` = SLA breached, `2` = error
  (see Part B4).
- The **reports** (`performance_report.html` / `.xlsx`) are saved as build
  artifacts — download them from the run to see per‑endpoint stats, orders
  created, and login success.

### Step C6 — Run in Docker inside CI (optional, no Python on the runner)

```bash
docker build -t apea .
docker run --rm \
  -e APEA_TARGET_URL=https://your-site.com \
  -e APEA_TEST_TYPE=smoke \
  -v "$PWD/projects:/app/projects" \
  apea python -m apea.cli --check-sla
```

See `PACKAGING.md` for more Docker and standalone options.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `'python' is not recognized` | Python isn't installed or not on PATH. Reinstall and tick "Add to PATH". |
| Browser doesn't open | Go to http://127.0.0.1:8000 manually. |
| "Locust is not installed" | Run `pip install -r requirements.txt` in the activated environment. |
| Port 8000 busy | Start with `python run.py --port 9000` and open that port. |
| YAML recording not read | Run `pip install pyyaml` (JMX and HAR work without it). |
| Test shows lots of failures | Normal for stress tests; for others, check the target URL is correct and reachable. |
| The run failed with an error popup | The message shows the cause; details are also written to `apea_error.log` in the project folder. |

---

## Quick reference

**Start the UI:** double-click `start.bat` (Windows) / `./start.sh` (Mac/Linux) →
open http://127.0.0.1:8000

**Run headless:**
```
python -m apea.cli --url <site> --test-type smoke --check-sla
```
