"""Time remaining, and what kind of thing was dropped as noise.

The last two panels from the approved design. Both read data the tool already
has; neither adds a measurement.

Two judgements worth keeping:

  * a run that has passed its duration but is still finishing must NOT say
    "0 minutes left". Sitting on zero for two minutes is worse than saying
    nothing, so it says "finishing".
  * a noise category with nothing in it is absent, not shown as zero. "Trackers
    0" reads as a finding; no chip reads as no trackers, which is the truth.

Run:  ./.venv/bin/python tests_last_panels.py     # expect FAILURES: 0
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


def fn_body(name):
    start = UI.index("function %s(" % name)
    nxt = UI.find("\nfunction ", start + 1)
    body = UI[start:nxt if nxt > 0 else len(UI)]
    return body[:body.rindex("}") + 1]


print("how much longer")
_t = fn_body("renderTimeLeft")
check("it is drawn on every poll", "renderTimeLeft(s, st)" in UI)
check("only while the run is going",
      "['starting','running'].includes(st)" in _t)
check("it uses the duration that was set", "s.duration_s" in _t)
check("and falls back to progress when there is none", "elapsed * 100 / s.pct" in _t)
check("an overrun says finishing, not zero", "'· finishing'" in _t)
check("the reason is recorded",
      "worse than saying nothing" in UI or "0 minutes left" in UI)
check("under 90 seconds it counts in seconds", "left >= 90" in _t)

print()
print("what was dropped, by kind")
_c = fn_body("renderCallSelector")
check("chips are built from the dropped list", "#noiseChips" in UI)
check("trackers are read from the filter's own reason",
      "/tracker|analytics|beacon/" in _c)
check("images from the path", "png|jpe?g|gif|svg" in _c)
check("fonts and stylesheets from the path", "woff2?|ttf|eot|otf|css" in _c)
check("an empty category is left out, not shown as zero",
      "order.filter(k=>buckets[k])" in _c)
check("the reason is recorded", "not shown rather than shown as zero" in _c)
check("nothing is estimated", "estimate" not in _c.lower()
      or "nothing here is estimated" in _c)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
