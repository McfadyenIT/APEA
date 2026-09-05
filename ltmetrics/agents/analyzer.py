"""Post-Run Intelligence & RCA Agent.

Parses Locust results into a JMeter-style summary, evaluates the SLA/CI gate,
generates prioritized AI recommendations, performs lightweight root-cause
analysis, and compares against the historical baseline for regression flags.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .. import db
from .filter import is_asset

# Static-asset label fallback: recorded labels drop the dot ("onepage.html" ->
# "onepagehtml"), so we can't rely on is_asset(path) for rows the endpoints_map
# doesn't cover. Match a trailing asset word on the label's last segment instead.
_ASSET_LABEL_RE = re.compile(
    r"(html|htm|css|mjs|png|jpe?g|gif|bmp|svg|webp|ico|woff2?|ttf|eot|otf|"
    r"mp4|webm|mov|avi|mp3|wav|wasm|pdf)$", re.I)


def _label_is_asset(name: str) -> bool:
    """True if a request LABEL looks like a static asset (used only when no real
    path is known for the row)."""
    tail = (name or "").split()[-1] if name else ""   # drop the "GET"/"POST" prefix
    tail = tail.rsplit("/", 1)[-1]                     # last path segment
    return bool(_ASSET_LABEL_RE.search(tail))

# Per-transaction P95 targets (ms) for common labels; fall back to plan target.
LABEL_SLA = {
    "Home Page": 800, "Login Page": 2000, "Login Submit": 2500,
    "Search Results": 3000, "Category (PLP)": 1000, "Product Detail (PDP)": 1000,
    "Cart Page": 2500, "Checkout Page": 3000, "Dashboard": 2000,
    "Pricing Page": 800,
}


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _read_stats(run_dir: Path) -> tuple[list[dict], dict | None]:
    path = run_dir / "results" / "locust_stats.csv"
    if not path.exists():
        return [], None
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    endpoints, aggregated = [], None
    for r in rows:
        rec = {
            "name": r.get("Name"),
            "method": r.get("Type") or "GET",
            "num_requests": int(_f(r.get("Request Count"))),
            "num_failures": int(_f(r.get("Failure Count"))),
            "avg": _f(r.get("Average Response Time")),
            "min": _f(r.get("Min Response Time")),
            "max": _f(r.get("Max Response Time")),
            "p50": _f(r.get("50%")),
            "p90": _f(r.get("90%")),
            "p95": _f(r.get("95%")),
            "p99": _f(r.get("99%")),
            "rps": _f(r.get("Requests/s")),
        }
        # Locust buckets its percentiles, so p99 can come back ABOVE the exact
        # max it was measured from. A percentile cannot exceed the maximum, and
        # a reader who spots that stops trusting the rest of the table.
        if rec["max"]:
            rec["p99"] = min(rec["p99"], rec["max"])
            rec["p95"] = min(rec["p95"], rec["max"])
        if rec["name"] == "Aggregated":
            aggregated = rec
        else:
            endpoints.append(rec)
    return endpoints, aggregated


def _read_failures(run_dir: Path) -> list[dict]:
    path = run_dir / "results" / "locust_failures.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [{"method": r.get("Method"), "name": r.get("Name"),
                 "error": r.get("Error"), "occurrences": int(_f(r.get("Occurrences")))}
                for r in csv.DictReader(fh)]


def _read_endpoints_map(run_dir: Path) -> dict:
    import json
    path = Path(run_dir) / "endpoints_map.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_browser_track(run_dir: Path) -> dict | None:
    """Track B (Playwright) results, if the run had a browser track. Reads the
    summary + per-transaction stats the runner wrote. None when Track B didn't run."""
    import json
    rd = Path(run_dir)
    summ_p = rd / "results" / "browser_track.json"
    stats_p = rd / "results" / "playwright_stats.csv"
    if not summ_p.exists() and not stats_p.exists():
        return None
    summary = {}
    try:
        summary = json.loads(summ_p.read_text(encoding="utf-8"))
    except Exception:
        summary = {}
    eps = []
    if stats_p.exists():
        try:
            with open(stats_p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("Name") == "Aggregated":
                        continue
                    eps.append({"name": r.get("Name"),
                                "num_requests": int(_f(r.get("Request Count"))),
                                "num_failures": int(_f(r.get("Failure Count"))),
                                "avg": _f(r.get("Average Response Time")),
                                "p95": _f(r.get("95%")),
                                "max": _f(r.get("Max Response Time"))})
        except Exception:
            pass
    return {"summary": summary, "endpoints": eps}


def _read_flow_stats(run_dir: Path) -> dict:
    """Login / order counters written by the generated flow script (if any)."""
    import json
    path = Path(run_dir) / "results" / "ltm_flow.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}



def is_transaction(e):
    """Is this row a transaction timer rather than a single request?

    A transaction wraps the calls inside it, so its time is their sum. Locust
    records ours with the type "TXN" and a "TXN: " name prefix; either alone is
    enough, because an older run may carry only one of them.
    """
    return (str(e.get("method") or "").strip().upper() == "TXN"
            or str(e.get("name") or "").strip().upper().startswith("TXN:"))


def analyze(run_dir: Path, plan_cfg: dict, discovery: dict,
            project_id: int, run_id: str) -> dict:
    endpoints, aggregated = _read_stats(Path(run_dir))
    failures = _read_failures(Path(run_dir))
    ep_map = _read_endpoints_map(Path(run_dir))
    flow_stats = _read_flow_stats(Path(run_dir))
    for e in endpoints:
        info = ep_map.get(e["name"], {})
        e["endpoint"] = info.get("path", "")
        e["req_method"] = info.get("method", e.get("method", ""))

    # Exclude static-asset rows (css/js/images/fonts/html/media) from the report
    # breakdown — they're noise in an API load report. Use the real path when the
    # endpoints_map knows it, else fall back to a label heuristic (older/unmapped
    # rows). The run's true aggregate totals are left untouched; we just record how
    # many endpoint rows were hidden so the report can note it.
    _api_eps, _static_eps = [], []
    for e in endpoints:
        p = e.get("endpoint") or ""
        static = is_asset(p) if p else _label_is_asset(e.get("name") or "")
        (_static_eps if static else _api_eps).append(e)
    endpoints = _api_eps
    static_excluded = {
        "count": len(_static_eps),
        "requests": sum(int(x.get("num_requests", 0)) for x in _static_eps),
        "names": [x.get("name") for x in _static_eps][:50],
    }

    if aggregated is None:
        total_req = sum(e["num_requests"] for e in endpoints)
        total_fail = sum(e["num_failures"] for e in endpoints)
        aggregated = {
            "name": "Aggregated", "num_requests": total_req,
            "num_failures": total_fail,
            "avg": max((e["avg"] for e in endpoints), default=0),
            "min": min((e["min"] for e in endpoints), default=0),
            "max": max((e["max"] for e in endpoints), default=0),
            "p50": max((e["p50"] for e in endpoints), default=0),
            "p90": max((e["p90"] for e in endpoints), default=0),
            "p95": max((e["p95"] for e in endpoints), default=0),
            "p99": max((e["p99"] for e in endpoints), default=0),
            "rps": sum(e["rps"] for e in endpoints),
        }

    total_req = aggregated["num_requests"]
    total_fail = aggregated["num_failures"]
    error_rate = (total_fail / total_req * 100) if total_req else 0.0

    overall = {
        "total_requests": total_req,
        "total_failures": total_fail,
        "error_rate": round(error_rate, 3),
        "avg_response": round(aggregated["avg"], 1),
        "p50": round(aggregated["p50"], 1),
        "p90": round(aggregated["p90"], 1),
        "p95": round(aggregated["p95"], 1),
        "p99": round(aggregated["p99"], 1),
        "max_response": round(aggregated["max"], 1),
        "throughput": round(aggregated["rps"], 2),
    }

    # ---- SLA gate ----------------------------------------------------------
    max_err = plan_cfg["exit_criteria"]["max_error_rate_pct"]
    max_p95 = plan_cfg["exit_criteria"]["max_p95_ms"]
    breaches = []
    for e in endpoints:
        # A transaction timer is the SUM of the calls inside it, so holding it to
        # a PER-REQUEST gate is arithmetic rather than a finding -- a journey of
        # twenty calls breaches a 5,000ms gate by existing, and then leads the
        # ticket as the latency hotspot. The stage panel already excludes these
        # for the same reason ("counting it would add the same milliseconds
        # twice"). They keep their row, their numbers and their failures; they
        # get no verdict against a gate that was never about them.
        if is_transaction(e):
            e["sla_target"] = None
            e["sla_pass"] = None
            continue
        target = LABEL_SLA.get(e["name"], max_p95)
        e["sla_target"] = target
        e["sla_pass"] = e["p95"] <= target
        if not e["sla_pass"]:
            breaches.append({"name": e["name"], "p95": e["p95"], "target": target})
    error_gate = error_rate <= max_err
    p95_gate = overall["p95"] <= max_p95
    sla_pass = error_gate and p95_gate and not breaches
    sla = {
        "pass": sla_pass,
        "error_gate": error_gate, "p95_gate": p95_gate,
        "max_error_rate_pct": max_err, "max_p95_ms": max_p95,
        "breaches": breaches,
    }

    # ---- Recommendations + RCA --------------------------------------------
    recommendations = _recommend(overall, endpoints, plan_cfg, error_rate, failures)
    rca = _rca(endpoints, failures, error_rate)
    llm_rca = _llm_narrative(run_dir, overall, sla, endpoints, failures, flow_stats)

    # ---- Trend vs baseline -------------------------------------------------
    trend = _trend(project_id, plan_cfg["test_type"], run_id, overall)

    jira = _jira_ticket(discovery, plan_cfg, overall, sla, rca) if not sla_pass else None

    return {
        "overall": overall,
        "endpoints": sorted(endpoints, key=lambda x: -x["num_requests"]),
        "static_excluded": static_excluded,
        "failures": failures,
        "sla": sla,
        "recommendations": recommendations,
        "rca": rca,
        "llm_rca": llm_rca,
        "trend": trend,
        "jira_markdown": jira,
        "plan": plan_cfg,
        "target": discovery.get("base_url"),
        "domain": discovery.get("domain"),
        "flow": flow_stats,
        "browser_track": _read_browser_track(Path(run_dir)),
    }


def _llm_narrative(run_dir, overall, sla, endpoints, failures, flow) -> str | None:
    """Optional Claude-written root-cause narrative. None if no API key."""
    try:
        from . import llm
        if not llm.available():
            return None
        import json
        top = sorted(endpoints, key=lambda x: -x.get("num_failures", 0))[:8]
        eps = [{"name": e.get("name"), "reqs": e.get("num_requests"),
                "fails": e.get("num_failures"), "p95": e.get("p95"),
                "avg": e.get("avg_response")} for e in top]
        repair = {}
        try:
            repair = json.loads((Path(run_dir) / "repair.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        prompt = (
            "Analyze this load-test run and explain the root cause, then give "
            "prioritized, specific, technical recommendations.\n\n"
            "OVERALL: " + json.dumps(overall)
            + "\nSLA: " + json.dumps(sla)
            + "\nTOP ENDPOINTS BY FAILURES: " + json.dumps(eps)
            + "\nCHECKOUT FLOW COUNTERS: " + json.dumps(flow)
            + "\nSAMPLE FAILURES: " + json.dumps((failures or [])[:15])
            + (("\nAI PRE-FLIGHT DIAGNOSIS: " + json.dumps(repair.get("diagnosis", "")))
               if repair.get("diagnosis") else "")
            + "\n\nWrite 4-8 concise sentences: what happened, the most likely cause, "
            "and the top fixes. No preamble."
        )
        return llm.chat(prompt, system="You are a senior performance engineering analyst.",
                        max_tokens=700)
    except Exception:
        return None


def _recommend(overall, endpoints, plan_cfg, error_rate, failures) -> list[dict]:
    recs: list[dict] = []
    max_p95 = plan_cfg["exit_criteria"]["max_p95_ms"]
    max_err = plan_cfg["exit_criteria"]["max_error_rate_pct"]

    if error_rate > max_err:
        top = sorted(endpoints, key=lambda x: -x["num_failures"])[:3]
        offenders = ", ".join(f"{e['name']} ({e['num_failures']})" for e in top if e["num_failures"])
        recs.append({
            "priority": "P1", "impact": "High", "effort": "Medium",
            "title": f"Error rate {error_rate:.2f}% exceeds {max_err}% budget",
            "detail": f"Investigate failing transactions: {offenders or 'see failures log'}. "
                      "Check app logs, upstream timeouts, and connection-pool limits.",
        })

    # sla_target is None for a transaction timer -- it wraps the calls inside it,
    # so there is no per-request target to be over. Read the target the gate
    # already decided rather than looking it up a second way.
    slow = [e for e in endpoints
            if e.get("sla_target") is not None and e["p95"] > e["sla_target"]]
    slow.sort(key=lambda x: -x["p95"])
    for e in slow[:3]:
        share = (e["num_requests"] / max(1, overall["total_requests"])) * 100
        impact = "High" if share > 20 else "Medium" if share > 5 else "Low"
        recs.append({
            "priority": "P2", "impact": impact, "effort": "Medium",
            "title": f"{e['name']} p95 {int(e['p95'])} ms over target "
                     f"{int(e['sla_target'])} ms",
            "detail": "Add server-side caching / CDN, review DB indexes and slow queries "
                      f"on this endpoint. Carries {share:.0f}% of traffic.",
        })

    if overall["throughput"] and overall["throughput"] < plan_cfg["expected_tps"] * 0.6:
        recs.append({
            "priority": "P2", "impact": "Medium", "effort": "High",
            "title": f"Throughput {overall['throughput']} TPS below expected "
                     f"~{plan_cfg['expected_tps']} TPS",
            "detail": "System may be saturating before target concurrency. Review CPU, "
                      "thread pools, and horizontal scaling / autoscaling policy.",
        })

    if not recs:
        recs.append({
            "priority": "P4", "impact": "Low", "effort": "Low",
            "title": "All SLAs met — healthy headroom",
            "detail": "No regressions detected. Consider a stress test to locate the "
                      "true breaking point and validate autoscaling.",
        })
    return recs


def _rca(endpoints, failures, error_rate) -> dict:
    if not endpoints:
        return {"summary": "No transaction data captured.", "suspects": []}
    by_fail = sorted(endpoints, key=lambda x: -x["num_failures"])
    # A transaction timer is the sum of the calls inside it, so it wins any
    # latency sort by construction and sends the reader to a number that is not
    # a bottleneck. Real endpoints only for the hotspot; if a run somehow has
    # nothing but transactions, fall back rather than report nothing.
    _real = [e for e in endpoints if not is_transaction(e)]
    by_slow = sorted(_real or endpoints, key=lambda x: -x["p95"])
    suspects = []
    if by_fail and by_fail[0]["num_failures"] > 0:
        suspects.append({
            "endpoint": by_fail[0]["name"],
            "reason": f"{by_fail[0]['num_failures']} failures "
                      f"({by_fail[0]['num_failures']}/{by_fail[0]['num_requests']} requests)",
        })
    if by_slow and by_slow[0]["p95"] > 0:
        suspects.append({
            "endpoint": by_slow[0]["name"],
            "reason": f"highest latency, p95 {int(by_slow[0]['p95'])} ms",
        })
    if error_rate == 0:
        summary = f"No failures. Latency leader: {by_slow[0]['name']} " \
                  f"(p95 {int(by_slow[0]['p95'])} ms)."
    else:
        summary = f"Primary suspect: {by_fail[0]['name']} with the most failures; " \
                  f"latency hotspot: {by_slow[0]['name']}."
    return {"summary": summary, "suspects": suspects,
            "top_failures": failures[:5]}


def _trend(project_id, test_type, run_id, overall) -> dict:
    base = db.previous_baseline(project_id, test_type, run_id)
    if not base:
        return {"has_baseline": False,
                "note": "First run of this profile — recorded as the new baseline."}
    def delta(cur, prev):
        if not prev:
            return None
        return round((cur - prev) / prev * 100, 1)
    p95_delta = delta(overall["p95"], base["p95"])
    err_delta = delta(overall["error_rate"], base["error_rate"])
    tps_delta = delta(overall["throughput"], base["throughput"])
    regression = (p95_delta is not None and p95_delta > 20) or \
                 (overall["error_rate"] - (base["error_rate"] or 0) > 1)
    return {
        "has_baseline": True,
        "baseline_run": base["id"],
        "baseline_date": base["started_at"],
        "p95_delta_pct": p95_delta,
        "error_delta_pct": err_delta,
        "throughput_delta_pct": tps_delta,
        "regression": regression,
        "note": ("⚠ Regression vs baseline" if regression
                 else "Within statistical tolerance of baseline."),
    }


def _jira_ticket(discovery, plan_cfg, overall, sla, rca) -> str:
    lines = [
        f"h2. [Performance] SLA breach — {discovery.get('base_url')} ({plan_cfg['label']})",
        "",
        "*Type:* Bug / Performance",
        "*Priority:* High",
        "",
        "h3. Summary",
        f"{plan_cfg['label']} against {discovery.get('base_url')} failed the CI quality gate.",
        "",
        "h3. Observed vs Target",
        "|| Metric || Observed || Target || Result ||",
        f"| Error rate | {overall['error_rate']}% | <= {sla['max_error_rate_pct']}% | "
        f"{'PASS' if sla['error_gate'] else 'FAIL'} |",
        f"| p95 latency | {int(overall['p95'])} ms | <= {int(sla['max_p95_ms'])} ms | "
        f"{'PASS' if sla['p95_gate'] else 'FAIL'} |",
        f"| Throughput | {overall['throughput']} TPS | ~{plan_cfg['expected_tps']} TPS | - |",
        "",
        "h3. Root Cause (preliminary)",
        rca["summary"],
    ]
    if sla["breaches"]:
        lines += ["", "h3. Endpoint breaches",
                  "|| Transaction || p95 (ms) || Target (ms) ||"]
        for b in sla["breaches"]:
            lines.append(f"| {b['name']} | {int(b['p95'])} | {int(b['target'])} |")
    return "\n".join(lines)
