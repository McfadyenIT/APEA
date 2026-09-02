"""The journey view: each stage opens to show the calls inside it.

The canvas draws Stage 1..6 as six separate panels. This renders them as rows
that expand instead, because rows let an operator COMPARE stages -- six panels
stacked down a page cannot be read against each other, and comparing is the
whole reason to break a run into stages.

Two numbers are deliberately side by side and must stay distinct:

  Time    what the stage cost in total
  Spans   how long it was in play, first call to last

On a concurrent run they differ by an order of magnitude -- Browse costs 17s
and spans 204s -- and reading one as the other is the easiest way to misread a
load test.

"Where the journey leaks" is NOT here and must not be added until a logged call
says which user made it. A drop-off drawn from call counts would look
authoritative and be wrong.

Run:  ./.venv/bin/python tests_journey_view.py     # expect FAILURES: 0
"""
import io
import sys

sys.path.insert(0, ".")

UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()
SRV = io.open("ltmetrics/server.py", encoding="utf-8").read()

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


print("a stage can be opened to see what is inside it")
check("the server returns the calls per stage", '"by_call"' in SRV)
check("largest first, so the cause is at the top",
      'key=lambda x: -x["ms"]' in SRV)
check("each call carries its own count, total, average and slowest",
      '"slowest": 0.0' in SRV and 'b["avg_ms"]' in SRV)
check("rows expand", "tr.calls[data-for=" in UI)
check("and the twisty tracks the state", "'+' : '\\\\u2212'" in UI
      or "body.hidden ? '+'" in UI)

print()
print("time spent and time spanned are kept apart")
check("the server measures the span", '"span_s"' in SRV)
check("from first call to last", 'a["first_ts"]' in SRV and 'a["last_ts"]' in SRV)
check("the table shows both", ">Time</th>" in UI and ">Spans</th>" in UI)
check("and the difference is explained where it is read",
      "how long it was in play" in UI)
check("the reason is recorded", "misread a load test" in UI)

print()
print("nothing claims to know who dropped out")
check("no funnel is drawn", "journey leaks" not in UI.lower())
check("no drop-off count", "drop-off" not in UI.lower()
      and "dropped out" not in UI.lower())
check("the gap is recorded rather than papered over",
      "which user" in SRV.lower() or "which user" in UI.lower())

print()
print("it stays general")
check("stages come from the run, not a fixed list",
      "STAGE_ORDER" not in UI and "Stage 1" not in UI)
check("colours cycle rather than being assigned per stage name",
      "i % STAGE_COLOURS.length" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
