# APEA CI/CD bundle — Radwell

Commit these files into your **application repository** (so CI runs against every
deployment):

```
ci/perf/recording.yaml
ci/perf/testdata.csv
perf-test.yml
.github/workflows/apea-performance.yml
```
(`ci/perf/locustfile.py` is the exact saved script, kept for reference — CI does
not need it; it regenerates from the recording so it self-heals to store changes.)

## Change the run configuration
Edit **perf-test.yml** — everything is plain English:
- `users:` concurrent virtual users
- `duration:` how long to hold load (e.g. `10m`, `30s`, `1h`)
- `workers:` parallel load generators
- `test type:` smoke / load / stress / spike / soak / …
- the pass/fail gate: `pass if error rate below:` and `pass if p95 below:`

## Add more test data (accounts)
Edit **ci/perf/testdata.csv** — one row per virtual user. Concurrent `users:` and
CSV rows are separate: to run N users with distinct accounts, add ~N rows (fewer
rows means accounts are reused round-robin, which can cause cart/quote contention).

## Run on every deployment (post-deploy trigger)
Have your deploy pipeline fire a GitHub `repository_dispatch` after a successful
deploy, e.g.:

```bash
curl -X POST \
  -H "Authorization: token $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<OWNER>/<REPO>/dispatches \
  -d '{"event_type":"deployed"}'
```

The job exits non-zero if the SLA gate fails, so a performance regression fails
the pipeline. Reports are uploaded as build artifacts.

Secrets to set in the repo (Settings → Secrets): `ANTHROPIC_API_KEY` (optional,
AI features), `PERF_USERNAME` / `PERF_PASSWORD` (if login creds shouldn't be in
the CSV).
