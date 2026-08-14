"""Export a saved script into a committable CI/CD bundle (full-APEA-pipeline mode).

Given a saved-script directory (saved_scripts/<name>/ with meta.json, the
generated locustfile, testdata.csv and the retained recording), produce a folder
the user commits to their app repo:

    <bundle>/
      ci/perf/<recording>            the recording (source of truth for CI)
      ci/perf/testdata.csv           the data CSV — edit rows here for more accounts
      ci/perf/locustfile.py          the exact saved script (reference)
      perf-test.yml                  plain-English config — edit users/duration/etc.
      .github/workflows/apea-performance.yml   post-deploy trigger
      README-CI.md                   commit + trigger instructions

In CI the run is: `python -m apea.cli --config perf-test.yml --check-sla`
(regenerates from the recording, self-heals, produces report + SLA gate).
Pure file operations — no framework deps; never raises silently-wrong output.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


def _human_duration(seconds) -> str:
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        s = 0
    if s <= 0:
        return "10m"
    if s % 3600 == 0:
        return "%dh" % (s // 3600)
    if s % 60 == 0:
        return "%dm" % (s // 60)
    return "%ds" % s


_WORKFLOW = """\
name: APEA Performance Test

# Runs the APEA performance test after every deployment. Trigger it from your
# deploy pipeline with a repository_dispatch of type "deployed" (see README-CI.md),
# or run it manually.
on:
  repository_dispatch:
    types: [deployed]
  workflow_dispatch:
    inputs:
      config:
        description: "Path to the plain-English config file"
        required: false
        default: "perf-test.yml"
        type: string

jobs:
  perf-test:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install APEA
        run: pip install -r requirements.txt
      - name: Run APEA performance test (fails the job if the SLA gate fails)
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          APEA_USERNAME: ${{ secrets.PERF_USERNAME }}
          APEA_PASSWORD: ${{ secrets.PERF_PASSWORD }}
        run: |
          python -m apea.cli --config "${{ github.event.inputs.config || 'perf-test.yml' }}" --check-sla | tee apea_output.txt
      - name: Upload reports
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: apea-performance-reports
          path: |
            projects/**/reports/performance_report.html
            projects/**/reports/performance_report.xlsx
            projects/**/results/locust_stats.csv
            apea_output.txt
          if-no-files-found: warn
"""


def _readme(name: str, rec_name: str) -> str:
    return f"""\
# APEA CI/CD bundle — {name}

Commit these files into your **application repository** (so CI runs against every
deployment):

```
ci/perf/{rec_name}
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
curl -X POST \\
  -H "Authorization: token $GH_TOKEN" \\
  -H "Accept: application/vnd.github+json" \\
  https://api.github.com/repos/<OWNER>/<REPO>/dispatches \\
  -d '{{"event_type":"deployed"}}'
```

The job exits non-zero if the SLA gate fails, so a performance regression fails
the pipeline. Reports are uploaded as build artifacts.

Secrets to set in the repo (Settings → Secrets): `ANTHROPIC_API_KEY` (optional,
AI features), `PERF_USERNAME` / `PERF_PASSWORD` (if login creds shouldn't be in
the CSV).
"""


def build_bundle(saved_dir, out_dir, overrides: dict | None = None) -> dict:
    """Assemble the CI bundle. Returns {ok, files, out_dir, warnings}."""
    saved_dir = Path(saved_dir)
    out_dir = Path(out_dir)
    overrides = overrides or {}
    warnings: list[str] = []
    files: list[str] = []
    if not saved_dir.exists():
        return {"ok": False, "error": "saved script not found: %s" % saved_dir}

    meta = {}
    try:
        meta = json.loads((saved_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        warnings.append("meta.json missing/unreadable — perf-test.yml uses defaults")

    perf = out_dir / "ci" / "perf"
    perf.mkdir(parents=True, exist_ok=True)
    (out_dir / ".github" / "workflows").mkdir(parents=True, exist_ok=True)

    # recording (required for full-pipeline CI)
    rec_name = ""
    rec_src = None
    rf = meta.get("recording_file")
    if rf and (saved_dir / rf).exists():
        rec_src = saved_dir / rf
    else:                                   # fall back to any recording-like file
        rec_src = next((p for p in saved_dir.glob("recording.*")), None)
    if rec_src and rec_src.exists():
        rec_name = rec_src.name
        shutil.copy(str(rec_src), str(perf / rec_name))
        files.append("ci/perf/%s" % rec_name)
    else:
        warnings.append("No recording retained with this saved script — add your "
                        "recording to ci/perf/ and set 'recording file:' in perf-test.yml. "
                        "(Re-save the script after this update to retain it automatically.)")

    # data CSV
    csv_src = saved_dir / "data" / "testdata.csv"
    if csv_src.exists():
        shutil.copy(str(csv_src), str(perf / "testdata.csv"))
        files.append("ci/perf/testdata.csv")
    else:
        warnings.append("No testdata.csv in the saved script.")

    # exact script (reference)
    ls = saved_dir / "scripts" / "locustfile.py"
    if ls.exists():
        shutil.copy(str(ls), str(perf / "locustfile.py"))
        files.append("ci/perf/locustfile.py")

    # perf-test.yml (prefilled from meta + overrides)
    users = overrides.get("users") or meta.get("users") or meta.get("expected_users") or 50
    duration = overrides.get("duration") or _human_duration(meta.get("duration_s"))
    workers = overrides.get("workers") or meta.get("workers") or 1
    ttype = overrides.get("test_type") or meta.get("test_type") or "load"
    target = meta.get("base_url") or ""
    rec_line = ("ci/perf/%s" % rec_name) if rec_name else ""
    perf_yml = (
        "# APEA performance test — plain-English config (edit values after the colon)\n"
        "# CI runs:  python -m apea.cli --config perf-test.yml --check-sla\n\n"
        "target url: %s\n" % target
        + "recording file: %s\n" % rec_line
        + "data file: ci/perf/testdata.csv\n\n"
        + "test type: %s\n" % ttype
        + "users: %s\n" % users
        + "duration: %s\n" % duration
        + "workers: %s\n\n" % workers
        + "cart quantity: 1\n"
        + "ai repair: false\n\n"
        + "# Pass/fail gate — the CI job exits non-zero if either is exceeded.\n"
        + "pass if error rate below: 1%\n"
        + "pass if p95 below: 3000 ms\n"
    )
    (out_dir / "perf-test.yml").write_text(perf_yml, encoding="utf-8")
    files.append("perf-test.yml")

    (out_dir / ".github" / "workflows" / "apea-performance.yml").write_text(
        _WORKFLOW, encoding="utf-8")
    files.append(".github/workflows/apea-performance.yml")

    (out_dir / "README-CI.md").write_text(
        _readme(meta.get("name", saved_dir.name), rec_name or "<your-recording>"),
        encoding="utf-8")
    files.append("README-CI.md")

    return {"ok": True, "out_dir": str(out_dir), "files": files, "warnings": warnings}
