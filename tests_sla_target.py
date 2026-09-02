"""Recommendations judge a run against ITS OWN SLA.

A passing Smoke Test carried "p95 4100ms over the 3000ms target" while the KPI
card beside it showed the same 4100 ms in green against 5000 ms. One report,
one number, two verdicts -- because the recommender used a module constant
(the Load Test figure) instead of the SLA the run was planned with.

These call the real builder rather than matching source text, so a future
regression that reintroduces the constant fails here.

Run:  ./.venv/bin/python tests_sla_target.py     # expect FAILURES: 0
"""
import sys

sys.path.insert(0, ".")

from apea.agents import recommendation  # noqa: E402
from apea.config import DEFAULT_P95_THRESHOLD_MS  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


def build(p95, err, sla):
    """Recommendation titles for one run."""
    analysis = {"overall": {"p95": p95, "error_rate": err},
                "endpoints": [], "sla": sla}
    return [r.get("title", "") for r in recommendation.build(analysis, {}, None)]


SMOKE = {"max_p95_ms": 5000, "max_error_rate_pct": 1.0}
LOAD = {"max_p95_ms": 3000, "max_error_rate_pct": 1.0}

print("latency is judged against the run's own target")

t = build(4100, 0.0, SMOKE)
check("4100 ms passes a Smoke Test (target 5000)",
      not any("over the" in x for x in t), "got: %s" % t)

t = build(4100, 0.0, LOAD)
check("the same 4100 ms fails a Load Test (target 3000)",
      any("over the 3000ms target" in x for x in t), "got: %s" % t)

t = build(5200, 0.0, SMOKE)
check("and a Smoke Test still fails when it really is over",
      any("over the 5000ms target" in x for x in t), "got: %s" % t)

check("the constant is only a fallback",
      "over the %.0fms target" % DEFAULT_P95_THRESHOLD_MS
      in " ".join(build(DEFAULT_P95_THRESHOLD_MS + 100, 0.0, {})))

print()
print("so is the error budget")

t = build(100, 0.5, {"max_p95_ms": 5000, "max_error_rate_pct": 2.0})
check("0.5% passes a 2% budget",
      not any("Error rate" in x for x in t), "got: %s" % t)

t = build(100, 2.5, {"max_p95_ms": 5000, "max_error_rate_pct": 2.0})
check("2.5% fails it", any("exceeds the 2.00% SLA" in x for x in t), "got: %s" % t)

t = build(100, 0.5, SMOKE)
check("and the default 1% budget still bites at 1.5%",
      any("Error rate" in x for x in build(100, 1.5, SMOKE))
      and not any("Error rate" in x for x in t))

print()
print("a missing SLA does not crash the report")
for bad in ({}, {"max_p95_ms": None}, {"max_p95_ms": ""}, None):
    try:
        recommendation.build({"overall": {"p95": 9000, "error_rate": 0},
                              "endpoints": [], "sla": bad}, {}, None)
        ok = True
    except Exception as exc:
        ok, detail = False, str(exc)[:60]
    check("sla=%r is survivable" % (bad,), ok, "" if ok else detail)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
