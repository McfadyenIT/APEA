# Running LT Metrics in CI/CD

LT Metrics runs the same performance test in a pipeline that you run locally — driven
by **one plain-English file, `perf-test.yml`**. There is no uploading in CI: you
commit the recording and data CSV to the repo, and the pipeline reads them by
path. Anyone on the team can change the test by editing `perf-test.yml` in plain
English and committing.

---

## 1. What you commit to the repo

```
your-repo/
├─ perf-test.yml            ← the plain-English config (edit this)
├─ ci/perf/
│  ├─ checkout.jmx          ← your BlazeMeter/JMeter recording (or .yaml/.har)
│  └─ testdata.csv          ← the data file LT Metrics told you to upload
├─ requirements.txt
└─ ltmetrics/                    ← the tool
```

Get `testdata.csv` from the app: upload your recording in the LT Metrics UI, click
**Analyze recording**, then **Download sample data CSV**, fill it in, and commit
it as `ci/perf/testdata.csv`.

---

## 2. Fill in `perf-test.yml`

Everything is natural language — no flags to memorise:

```yaml
target url: https://your-store.example.com   # blank = use the recording's URL
recording file: ci/perf/checkout.jmx
test type: load          # smoke | baseline | load | stress | spike | soak | capacity | volume | failover
users: 50
duration: 10m            # s / m / h
workers: 1
data file: ci/perf/testdata.csv
pass if error rate below: 1%
pass if p95 below: 3000 ms
```

That's the whole configuration. Commit it.

---

## 3a. GitHub Actions

The workflow is already at `.github/workflows/performance-tests.yml`. It runs:

- automatically when you push a change to `perf-test.yml` (or the tool), and
- on demand from the **Actions** tab (Run workflow).

Optional one-time setup under **Settings → Secrets and variables → Actions**:

- `PERF_USERNAME` / `PERF_PASSWORD` — only if the journey logs in.
- `ANTHROPIC_API_KEY` — only if `ai repair: true` or you want AI analysis.

After a run, open the run and download the **ltmetrics-performance-reports** artifact
(HTML report, Excel, stats, and the full failures CSV). The build fails if the
pass/fail criteria in `perf-test.yml` are breached.

## 3b. Jenkins

Point a Pipeline job at `ci/Jenkinsfile`. One-time setup:

- Add a **username/password** credential with id `perf-creds` (used for logins).
- Install the **HTML Publisher** plugin (to view the report in Jenkins).

Run the job (optionally change the `CONFIG` parameter if your file isn't
`perf-test.yml`). The report is published as **LT Metrics Performance Report** and the
build is gated on the pass/fail criteria.

## 3c. Docker (any CI)

```
docker build -t ltmetrics .
docker run --rm -v "$PWD:/work" -w /work ltmetrics \
    python -m ltmetrics.cli --config perf-test.yml --check-sla
```

---

## 4. What "no uploading" means

Because the recording and data CSV are committed files referenced by path in
`perf-test.yml`, the pipeline needs nothing at run time except the repo. Update
the test by editing those files and committing — the next run picks them up.

## 5. Exit code

`--check-sla` makes LT Metrics exit non-zero when the pass/fail criteria are breached,
so the pipeline stage fails and blocks the build/deploy. Remove the pass/fail
lines from `perf-test.yml` (or drop `--check-sla`) to run without gating.
