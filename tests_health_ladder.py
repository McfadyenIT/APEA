"""The health ladder: when a stage stops being fine.

Healthy / Watch / Strained / Failing / Critical carry the whole verdict, so the
thresholds behind them were derived from the approved design's own numbers
against its own 2,000ms gate rather than picked:

    300ms  15%  Healthy      1690ms  84%  Strained
    612ms  31%  Healthy      2104ms 105%  Failing
   1204ms  60%  Watch

which puts the rungs at half the gate, four fifths, and the gate itself.

Three things this file holds:

  * the ladder is a RATIO of the run's own gate, never absolute milliseconds.
    800ms and 5,000ms are both somebody's real target and a fixed threshold
    would call one of them wrong.
  * the gate comes from what the RUN stored, not from the setup form. A run
    made last week was judged against last week's target; re-judging it against
    today's would quietly rewrite what a client was told.
  * a 5xx escalates to Critical whatever the latency says. Being slow and being
    broken are different problems.

Run:  ./.venv/bin/python tests_health_ladder.py     # expect FAILURES: 0
"""
import io
import sys

sys.path.insert(0, ".")

import yaml  # noqa: E402

from ltmetrics.server import _health_ladder, _rate  # noqa: E402

UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()
SRV = io.open("ltmetrics/server.py", encoding="utf-8").read()

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


L = _health_ladder("")

print("the rungs reproduce the design's own examples")
GATE = 2000
for ms, expect in ((300, "Healthy"), (612, "Healthy"), (1204, "Watch"),
                   (1690, "Strained"), (2104, "Failing")):
    got = _rate(ms, GATE, 0, 0, L)
    check("%5dms (%3.0f%%) is %s" % (ms, 100.0 * ms / GATE, expect),
          got == expect, "got %s" % got)

print()
print("it is a ratio, not a millisecond figure")
check("400ms against an 800ms gate is Healthy — exactly on the rung",
      _rate(400, 800, 0, 0, L) == "Healthy", _rate(400, 800, 0, 0, L))
check("and 700ms against 800ms is Strained",
      _rate(700, 800, 0, 0, L) == "Strained", _rate(700, 800, 0, 0, L))
check("3,000ms against an 800ms gate is Critical",
      _rate(3000, 800, 0, 0, L) == "Critical")
check("no gate means no claim", _rate(3000, 0, 0, 0, L) == "")

print()
print("broken is not the same as slow")
check("a 5xx is Critical even when fast",
      _rate(100, 5000, 1, 1, L) == "Critical")
check("a fast stage with non-server errors is Failing, not Healthy",
      _rate(100, 5000, 3, 0, L) == "Failing")
check("and clean and fast is Healthy", _rate(100, 5000, 0, 0, L) == "Healthy")

print()
print("the thresholds are data, not code")
KBY = yaml.safe_load(io.open("ltmetrics/knowledge/rules/platform_rules.yaml",
                             encoding="utf-8"))
check("they live in the knowledge base",
      "health_ladder" in (KBY.get("generic") or {}))
check("every platform inherits them",
      _health_ladder("shopify") == _health_ladder("magento"))
check("a platform may still override", "KB.platform_rules(block)" in SRV)

print()
print("the gate is the run's own")
check("read from the run's stored SLA targets",
      'e.get("sla_target")' in SRV)
check("not from the setup form",
      "ovSlaP95" not in SRV)
check("the reason is recorded",
      "rewrite what the operator was told" in SRV
      or "quietly rewrite" in SRV)

print()
print("the page shows what a rung was measured against")
check("the key is on screen", 'id="ladderKey"' in UI)
check("with the thresholds", "L.healthy_pct" in UI and "L.strained_pct" in UI)
check("and the run's own gate", "of this run" in UI)
check("the worst rung is named, not left to be spotted",
      "is '\n              + _escHtml(worst.health.toLowerCase())" in UI
      or "worst.health.toLowerCase()" in UI)
check("a run with no gate makes no claim",
      "no gate set" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
