"""Test Planning Agent.

Translate plain-English intent and business metrics into an explicit technical
workload profile for any of the eight supported test types.
"""
from __future__ import annotations

from ..config import DEFAULT_ERROR_RATE_THRESHOLD, DEFAULT_P95_THRESHOLD_MS

# Test-type templates. Values are multipliers/base numbers relative to the
# user's "normal expected users". They are sane, non-technical-friendly defaults
# that the user can override in the UI.
TEST_TYPES = {
    "smoke": {
        "label": "Smoke Test",
        "purpose": "Validate the script and endpoints work with minimal load.",
        "users_factor": 0.02, "min_users": 5, "duration_s": 120,
        "ramp_factor": 1.0, "error_threshold": 1.0, "p95_ms": 5000,
    },
    "baseline": {
        "label": "Baseline Test",
        "purpose": "Establish reference performance under light, steady load.",
        "users_factor": 0.25, "min_users": 10, "duration_s": 300,
        "ramp_factor": 0.5, "error_threshold": 1.0, "p95_ms": 3000,
    },
    "load": {
        "label": "Load Test",
        "purpose": "Validate performance under expected normal peak traffic.",
        "users_factor": 1.0, "min_users": 20, "duration_s": 600,
        "ramp_factor": 0.2, "error_threshold": 1.0, "p95_ms": 3000,
    },
    "stress": {
        "label": "Stress Test",
        "purpose": "Push beyond normal limits to find the breaking point.",
        "users_factor": 2.5, "min_users": 50, "duration_s": 1200,
        "ramp_factor": 0.1, "error_threshold": 5.0, "p95_ms": 8000,
    },
    "spike": {
        "label": "Spike Test",
        "purpose": "Slam the system with sudden traffic (flash-sale style).",
        "users_factor": 3.0, "min_users": 50, "duration_s": 300,
        "ramp_factor": 1.0, "error_threshold": 5.0, "p95_ms": 8000,
    },
    "soak": {
        "label": "Soak / Endurance Test",
        "purpose": "Sustained load over time to reveal memory leaks / degradation.",
        "users_factor": 1.0, "min_users": 20, "duration_s": 3600,
        "ramp_factor": 0.15, "error_threshold": 1.0, "p95_ms": 3000,
    },
    "capacity": {
        "label": "Capacity / Scalability Test",
        "purpose": "Step load up to map capacity headroom.",
        "users_factor": 2.0, "min_users": 50, "duration_s": 900,
        "ramp_factor": 0.05, "error_threshold": 2.0, "p95_ms": 5000,
    },
    "volume": {
        "label": "Volume Test",
        "purpose": "Production-scale data volume under normal concurrency.",
        "users_factor": 1.0, "min_users": 20, "duration_s": 1800,
        "ramp_factor": 0.15, "error_threshold": 1.0, "p95_ms": 4000,
    },
    "failover": {
        "label": "Failover / Resilience Test",
        "purpose": "Hold steady load while a component fails over, to verify "
                   "recovery, graceful errors, and no data loss.",
        "users_factor": 1.0, "min_users": 20, "duration_s": 1200,
        "ramp_factor": 0.15, "error_threshold": 5.0, "p95_ms": 8000,
    },
    "test_plan": {
        "label": "Test Plan (recommended suite)",
        "purpose": "An AI-recommended sequence of tests to run for this application; "
                   "if executed directly it behaves like a standard load test.",
        "users_factor": 1.0, "min_users": 20, "duration_s": 600,
        "ramp_factor": 0.2, "error_threshold": 1.0, "p95_ms": 3000,
    },
}

ALL_TYPES = list(TEST_TYPES.keys())


def _humanize(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def plan(test_type: str, expected_users: int = 100, peak_users: int | None = None,
         overrides: dict | None = None) -> dict:
    """Build a full technical workload profile from simple inputs."""
    test_type = (test_type or "load").lower()
    tpl = TEST_TYPES.get(test_type, TEST_TYPES["load"])
    overrides = overrides or {}

    peak_users = peak_users or int(expected_users * 5)
    reference = peak_users if test_type in ("stress", "spike", "capacity") else expected_users
    if test_type == "failover":
        reference = expected_users

    users = max(tpl["min_users"], int(round(reference * tpl["users_factor"])))
    users = int(overrides.get("users", users))

    duration_s = int(overrides.get("duration_s", tpl["duration_s"]))

    # spawn rate: ramp over ramp_factor * duration (immediate for spike/smoke)
    ramp_window = max(1, int(duration_s * tpl["ramp_factor"]))
    if tpl["ramp_factor"] >= 1.0:
        ramp_window = max(1, min(10, users // 10 or 1))
    spawn_rate = round(max(1.0, users / ramp_window), 2)
    spawn_rate = float(overrides.get("spawn_rate", spawn_rate))

    # Think time / pacing: 2-5s default browse pacing (1-3 under stress/spike),
    # overridable so a test plan's think time flows through to the workload.
    think_min, think_max = 2, 5
    if test_type in ("stress", "spike"):
        think_min, think_max = 1, 3
    try:
        if overrides.get("think_min") is not None:
            think_min = int(float(overrides["think_min"]))
        if overrides.get("think_max") is not None:
            think_max = int(float(overrides["think_max"]))
        if think_max < think_min:
            think_max = think_min
    except (TypeError, ValueError):
        pass

    # Rough expected TPS: users / average pacing (think + ~0.5s service).
    avg_cycle = (think_min + think_max) / 2 + 0.5
    expected_tps = round(users / avg_cycle, 1)

    error_threshold = float(overrides.get("error_threshold",
                                          tpl.get("error_threshold",
                                                  DEFAULT_ERROR_RATE_THRESHOLD)))
    p95_threshold = float(overrides.get("p95_ms",
                                        tpl.get("p95_ms", DEFAULT_P95_THRESHOLD_MS)))

    # pacing = one full user cycle (think + service); arrival rate (open-model
    # equivalent) = iterations/sec at steady state ≈ users / cycle.
    pacing_s = round(avg_cycle, 1)
    arrival_rate = round(users / avg_cycle, 2) if avg_cycle else float(users)

    return {
        "test_type": test_type,
        "label": tpl["label"],
        "purpose": tpl["purpose"],
        "users": users,
        "peak_users": peak_users,
        "spawn_rate": spawn_rate,
        # Ramp-up
        "ramp_window_s": ramp_window,
        "ramp_up_s": ramp_window,
        "ramp_up_human": _humanize(ramp_window),
        # Duration / Run time
        "duration_s": duration_s,
        "duration_human": _humanize(duration_s),
        "run_time_s": duration_s,
        "run_time_human": _humanize(duration_s),
        # Think time + Pacing
        "think_time": [think_min, think_max],
        "pacing_s": pacing_s,
        "pacing_human": f"{think_min}–{think_max}s between actions (~{pacing_s}s/cycle)",
        # Throughput
        "expected_tps": expected_tps,
        "arrival_rate_per_s": arrival_rate,
        "exit_criteria": {
            "max_error_rate_pct": error_threshold,
            "max_p95_ms": p95_threshold,
        },
        "summary": (
            f"{tpl['label']}: {users} concurrent users, ramp {spawn_rate}/s over "
            f"~{_humanize(ramp_window)}, hold for {_humanize(duration_s)}. "
            f"Pass if error rate < {error_threshold}% and p95 < {int(p95_threshold)} ms."
        ),
    }


def ai_suggest(discovery: dict, test_type: str, expected_users: int,
               peak_users: int | None, current: dict | None = None) -> dict:
    """AI-tuned planning. Returns {notes, suggested:{...}, plan_sequence:[...]}.

    `notes` = plain-language rationale. `suggested` = numeric overrides the UI can
    apply (users/duration_s/spawn_rate/think_min/think_max). `plan_sequence` = for
    the 'test_plan' type, an ordered suite recommendation. No key → empty result.
    """
    out = {"notes": None, "suggested": {}, "plan_sequence": []}
    try:
        from . import llm
        if not llm.available():
            return out
        import json
        ctx = {
            "test_type": test_type,
            "expected_users": expected_users,
            "peak_users": peak_users,
            "domain": discovery.get("domain"),
            "tech": discovery.get("tech"),
            "journeys": [{"name": j.get("name"), "weight": j.get("weight")}
                         for j in (discovery.get("journeys") or [])],
            "current_plan": {k: (current or {}).get(k) for k in
                             ("users", "duration_s", "spawn_rate", "think_time",
                              "expected_tps")} if current else None,
        }
        want_seq = (test_type or "").lower() == "test_plan"
        prompt = (
            "As a performance test architect, tune the workload for this app. "
            + json.dumps(ctx, default=str)
            + "\n\nReturn JSON with: \"notes\" (2-4 sentences of rationale), "
            "\"suggested\" (object with any of: users, duration_s, spawn_rate, "
            "think_min, think_max — realistic integers/floats for THIS test type), "
            + ("and \"plan_sequence\" (ordered array of {test_type, users, "
               "duration_s, why} covering smoke→baseline→load→stress→spike→soak as "
               "appropriate for this app)." if want_seq else "\"plan_sequence\": [].")
        )
        res = llm.json_call(prompt, system="You design realistic load-test workload "
                            "models.", max_tokens=1100) or {}
        if isinstance(res.get("notes"), str):
            out["notes"] = res["notes"]
        sug = res.get("suggested")
        if isinstance(sug, dict):
            clean = {}
            for k in ("users", "duration_s"):
                if isinstance(sug.get(k), (int, float)) and sug[k] > 0:
                    clean[k] = int(sug[k])
            for k in ("spawn_rate", "think_min", "think_max"):
                if isinstance(sug.get(k), (int, float)) and sug[k] > 0:
                    clean[k] = float(sug[k])
            out["suggested"] = clean
        if isinstance(res.get("plan_sequence"), list):
            out["plan_sequence"] = res["plan_sequence"][:8]
    except Exception:
        pass
    return out


def workload_model(discovery: dict, plan_cfg: dict) -> dict:
    """Derive per-journey user division and task weights from discovery."""
    journeys = discovery.get("journeys", [])
    total_users = plan_cfg["users"]
    rows = []
    for j in journeys:
        weight = j.get("weight", 0)
        vus = round(total_users * weight / 100)
        rows.append({
            "journey": j.get("name", "Journey"),
            "weight_pct": weight,
            "concurrent_users": vus,
            "steps": j.get("steps", []),
        })
    return {
        "total_users": total_users,
        "division": rows,
        "think_time": plan_cfg["think_time"],
    }
