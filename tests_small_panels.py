"""The four presentation items from 9a that needed no new capability.

  Hide dropped calls        the dropped list is 128 rows in 9a's example
  Bars / Lines              9a offers both for the throughput series
  run <id> · <date>         so a screenshot of a result identifies itself
  The journey, live         the same ladder, against a log still being written

The live journey is the one worth holding: it reads the SAME stages endpoint
phases 3 and 4 use. A partial call log gives partial stages, which is exactly
what "filling up" means -- nothing extra is measured, and there is no second
code path to drift out of step with the finished view.

Run:  ./.venv/bin/python tests_small_panels.py     # expect FAILURES: 0
"""
import io
import sys

sys.path.insert(0, ".")

UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


print("hide dropped calls")
check("the toggle exists", 'id="hideDropped"' in UI)
check("it hides the list, not the chips",
      "box.style.display = hd.checked ? 'none' : ''" in UI)
check("on by default, because the list is long", 'id="hideDropped"' in UI
      and 'style="width:auto" checked' in UI)

print()
print("bars / lines")
check("both are offered", 'data-mode="bar"' in UI and 'data-mode="line"' in UI)
check("only the throughput series changes", "type:CHART_MODE" in UI)
check("the chart is rebuilt, not mutated in place",
      "initLiveChart();" in UI.split("CHART_MODE = b.dataset.mode")[1][:400])
check("the reason is recorded", "drawn twice" in UI)
check("data survives the switch", "JSON.parse(JSON.stringify(liveChart.data))" in UI)

print()
print("a result says which run it came from")
check("the id is shown", 'id="resRunId"' in UI)
check("with a readable time", "toLocaleTimeString" in UI)
check("drawn when results are", "renderRunId();" in UI)

print()
print("the journey fills up as it runs")
check("there is a place for it", 'id="liveJourney"' in UI)
check("it reads the same endpoint as phases 3 and 4",
      "'/api/run/' + currentRun + '/stages'" in UI)
check("it shows the ladder, not a second scheme", "rung-' + (r.health" in UI)
check("overlapping polls cannot pile up", "_journeyBusy" in UI)
check("and it says so before the first stage arrives",
      "Stages appear here as the run reaches them" in UI)
check("the reason is recorded", "partial log gives partial stages" in UI
      or "is what" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
