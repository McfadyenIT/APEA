"""Performance Planner — merge intent + discovery into one execution contract.

This is the module that turns the QA engineer's SELECTED test type into a concrete,
authoritative execution profile by combining four inputs:

    Business Flow          (from the recording / discovery)
    Application Knowledge   (Phase 3 — optional today)
    Selected Test Type      (chosen by the user from the Test Plan Analyzer's list)
    Performance Test Plan   (test-plan.json from test_plan_analyzer)

and emitting ONE machine contract — execution-plan.json — that carries the plan's
own numbers (users, duration, ramp-up/down, think time, SLA, pass criteria) rather
than falling back to generic defaults. The generator/executor then run to THIS plan.

It reuses the existing agents.planner for any value the test plan leaves blank, so a
sparse plan still yields a complete, runnable profile. Platform-independent; never
raises; a missing test plan degrades to the planner's defaults for the chosen type.
"""
from __future__ import annotations

from .test_plan_analyzer import _dur_seconds  # shared human-duration parser

_TYPES = ("smoke", "load", "stress", "spike", "soak")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick_test(test_plan: dict, selected_type: str) -> dict:
    """The plan entry for the chosen type, or an empty stub if the plan didn't list it."""
    sel = (selected_type or "").lower()
    for t in (test_plan.get("tests") or []):
        if str(t.get("type") or "").lower() == sel:
            return t
    return {"type": sel or "load", "name": (sel or "load").capitalize()}


def build(test_plan: dict | None, selected_type: str,
          business_flow: dict | None = None,
          app_knowledge: dict | None = None) -> dict:
    """Build execution-plan.json from the test plan + the selected test type (+ the
    business flow). Values come from the plan where present; anything absent is left
    None for agents.planner to fill in with a sensible default for this test type."""
    try:
        tp = test_plan or {}
        sel = (selected_type or "load").lower()
        if sel not in _TYPES:
            sel = "load"
        t = _pick_test(tp, sel)

        dur_s = _num(t.get("duration_s")) or (_dur_seconds(t.get("duration")))
        ramp_up_s = _dur_seconds(t.get("ramp_up"))
        ramp_down_s = _dur_seconds(t.get("ramp_down"))
        tt = tp.get("think_time") or {}
        sla = tp.get("sla") or {}
        bf = business_flow or {}

        return {
            "test_type": sel,
            "name": t.get("name") or sel.capitalize(),
            "users": _num(t.get("users")),
            "peak_users": _num(t.get("peak_users")),
            "duration_s": int(dur_s) if dur_s else None,
            "duration_human": t.get("duration"),
            "ramp_up_s": int(ramp_up_s) if ramp_up_s else None,
            "ramp_down_s": int(ramp_down_s) if ramp_down_s else None,
            "think_time": {"min_s": tt.get("min_s"), "max_s": tt.get("max_s")},
            "sla": {
                "p95_ms": sla.get("p95_ms"),
                "error_rate_pct": sla.get("error_rate_pct"),
                "throughput_rps": sla.get("throughput_rps"),
            },
            "business_flow": {
                "name": bf.get("name") or "recorded flow",
                "steps": (bf.get("steps") or [])[:30],
            },
            "objectives": tp.get("objectives") or [],
            "pass_criteria": tp.get("pass_criteria") or [],
            "business_scenarios": tp.get("business_scenarios") or [],
            "target_environment": tp.get("target_environment"),
            "source": "test-plan + selection" if tp.get("tests") else "defaults",
            "notes": tp.get("notes") or "",
        }
    except Exception as exc:
        return {"test_type": (selected_type or "load").lower(), "source": "error",
                "notes": "planner error: %s" % exc, "sla": {}, "think_time": {},
                "business_flow": {}, "users": None, "duration_s": None}


def to_overrides(execution_plan: dict | None) -> dict:
    """Extract the values agents.planner.plan() accepts as overrides, so the plan's
    numbers drive the technical workload. Only includes values the plan actually
    specified — everything else falls through to the planner's defaults."""
    ep = execution_plan or {}
    ov = {}
    try:
        if ep.get("users") is not None:
            ov["users"] = int(float(ep["users"]))
        if ep.get("duration_s") is not None:
            ov["duration_s"] = int(float(ep["duration_s"]))
        sla = ep.get("sla") or {}
        if sla.get("p95_ms") is not None:
            ov["p95_ms"] = float(sla["p95_ms"])
        if sla.get("error_rate_pct") is not None:
            ov["error_threshold"] = float(sla["error_rate_pct"])
    except Exception:
        pass                              # a hand-built/non-numeric plan must never crash the run
    return ov


def apply_to_plan_cfg(plan_cfg: dict, execution_plan: dict | None,
                      skip_think: bool = False, skip_sla: bool = False) -> dict:
    """Merge the plan's think time + SLA into an already-built plan_cfg, as a
    FALLBACK for values the user didn't set explicitly. When the caller already
    applied an explicit think-time / SLA override (skip_think / skip_sla), that
    wins and is left untouched. Users/duration are handled via to_overrides() at
    plan() time. Mutates + returns plan_cfg; never raises."""
    try:
        ep = execution_plan or {}
        tt = ep.get("think_time") or {}
        if not skip_think and tt.get("min_s") is not None:
            plan_cfg["think_time"] = [tt["min_s"], tt.get("max_s") or tt["min_s"]]
        sla = ep.get("sla") or {}
        ec = plan_cfg.setdefault("exit_criteria", {})
        if not skip_sla and sla.get("p95_ms") is not None:
            ec["max_p95_ms"] = sla["p95_ms"]
        if not skip_sla and sla.get("error_rate_pct") is not None:
            ec["max_error_rate_pct"] = sla["error_rate_pct"]
        plan_cfg["execution_plan"] = ep
    except Exception:
        pass
    return plan_cfg
