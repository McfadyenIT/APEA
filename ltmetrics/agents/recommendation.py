"""Recommendation step — a first-class stage after the Analyzer.

Turns raw analysis + the live checkout state + the Knowledge Base + RCA memory
into concrete, prioritised recommendations an engineer can act on. Deterministic
(no LLM); every recommendation is grounded in evidence from the run or in a KB
rule. Never raises — returns a list of {severity, area, issue, recommendation, ...}.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import db, memory
from ..config import DEFAULT_ERROR_RATE_THRESHOLD, DEFAULT_P95_THRESHOLD_MS
from ..knowledge import KB


def _fix_text(bug: dict) -> str:
    rid = (bug or {}).get("repair")
    return next((r.get("explanation", "") for r in KB.repair_rules()
                 if r.get("id") == rid), "")


def _read_flow(run_dir) -> dict:
    try:
        p = Path(run_dir) / "results" / "ltm_flow.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _rec(priority: str, title: str, impact: str, effort: str, detail: str) -> dict:
    """The recommendation shape the UI and report renderers expect."""
    return {"priority": priority, "title": title, "impact": impact,
            "effort": effort, "detail": detail}


def build(analysis: dict, discovery: dict | None = None, run_dir=None) -> list[dict]:
    """KB/RCA/checkout-grounded recommendations, in the {priority,title,impact,
    effort,detail} shape the UI + report use. Merged (not replacing) with the
    analyzer's performance recommendations by the caller."""
    recs: list[dict] = []
    try:
        platform = memory.detect_platform((discovery or {}).get("base_url", ""))
        flow = _read_flow(run_dir) if run_dir else {}
        cs = flow.get("checkout_state") or {}

        # functional / checkout — grounded in the state machine + KB
        reason = str(cs.get("stop_reason") or cs.get("abort_reason") or "")
        if reason:
            bug = KB.classify_error(reason)
            if bug:
                note = ""
                prior = db.recall_rca(bug.get("id", ""), platform)
                if prior and prior.get("occurrences", 0) > 1:
                    note = " (seen %dx before on %s)" % (prior["occurrences"], platform)
                recs.append(_rec(
                    "P1", "Checkout blocked: %s" % bug.get("summary", "checkout failure"),
                    "High", "Low",
                    "%s Fix: %s.%s" % (bug.get("root_cause", ""),
                                       _fix_text(bug) or "see knowledge base", note)))
            else:
                recs.append(_rec(
                    "P1", "Checkout stopped at %s" % (cs.get("stopped_at") or "an early step"),
                    "High", "Medium",
                    "Unrecognised failure: %s. Add a signature to known_bugs.yaml so it's "
                    "auto-diagnosed next time." % reason[:200]))
        elif flow and not flow.get("orders"):
            recs.append(_rec("P2", "No order created", "High", "Medium",
                             "Run completed without creating an order — review the checkout "
                             "state report / timeline."))

        # reliability + latency — grounded in the metrics
        o = analysis.get("overall") or {}
        err = float(o.get("error_rate", 0) or 0)
        p95 = float(o.get("p95", 0) or 0)
        # Judge against the SLA THIS run was planned with, not a module
        # constant. A Smoke Test targets 5000 ms and a Load Test 3000 ms, so
        # the constant made a passing smoke run carry "p95 4100ms over the
        # 3000ms target" while the KPI beside it showed the same number green
        # against 5000 -- one report, one number, two verdicts.
        _sla = analysis.get("sla") or {}
        p95_target = float(_sla.get("max_p95_ms") or DEFAULT_P95_THRESHOLD_MS)
        err_target = float(_sla.get("max_error_rate_pct")
                           or DEFAULT_ERROR_RATE_THRESHOLD)
        if err > err_target:
            recs.append(_rec("P1", "Error rate %.2f%% exceeds the %.2f%% SLA"
                             % (err, err_target), "High", "Medium",
                             "Fix the top failing endpoint before scaling load."))
        if p95 > p95_target:
            recs.append(_rec("P2", "p95 %.0fms over the %.0fms target"
                             % (p95, p95_target), "Medium", "Medium",
                             "Profile the slowest endpoints; check DB / cache / N+1 queries."))
        eps = analysis.get("endpoints") or []
        slow = max(eps, key=lambda e: float(e.get("p95", 0) or 0), default=None)
        if slow and float(slow.get("p95", 0) or 0) > 0:
            recs.append(_rec("P3", "Slowest endpoint: %s (p95 %.0fms)"
                             % (slow.get("name", "?"), float(slow.get("p95", 0))),
                             "Low", "Low", "Start the latency investigation here."))
    except Exception:
        pass
    return recs
