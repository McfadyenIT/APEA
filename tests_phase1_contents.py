"""Phase 1's panels, per the canvas.

The frame matched the design; the panels inside it did not, and the operator
spotted it by putting the two screens side by side. This covers the parts that
need no new measurement.

What is deliberately NOT here, and must stay absent until the data exists:
the noise chips with per-category counts, and the script-review score. Both
would put a confident number on screen with nothing behind it.

Run:  ./.venv/bin/python tests_phase1_contents.py     # expect FAILURES: 0
"""
import io
import sys

sys.path.insert(0, ".")

UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()

FAILURES = []


def fn_body(name):
    start = UI.index("function %s(" % name)
    nxt = UI.find("\nfunction ", start + 1)
    body = UI[start:nxt if nxt > 0 else len(UI)]
    return body[:body.rindex("}") + 1]


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


print("the screen states the question it is asking")
check("the eyebrow is there",
      "Review what we found, then choose the traffic" in UI)
check("and the question", "Check the plan, then set the\n        traffic." in UI
      or "Check the plan, then set the" in UI)

print()
print("traffic is chosen by shape of day, not by raw numbers alone")
for label in ("A normal day", "A busy day", "Find the breaking point"):
    check("preset %r" % label, label in UI)
check("a preset fills the fields rather than hiding them",
      "u.value = b.dataset.users" in UI and "d.value = String(+b.dataset.mins * 60)" in UI)
check("the operator can still type their own number",
      "addEventListener('input', renderPlanSummary)" in UI)
check("the breaking point switches the TYPE, not the number",
      "/stress/i.test" in UI)
check("the reason is recorded", "different SHAPE of test" in UI)

print()
print("what will happen is said in words")
check("the rail carries a summary", 'id="railSummary"' in UI)
check("and one plain sentence", 'id="railWill"' in UI)
check("built from what is actually set", "function _planNow()" in UI)
check("the run button names what it will do",
      "Start the run \\u2014 ' + _escHtml(d)" in UI)
check("the reason is recorded",
      "300 users believing they set 3" in UI or "thinking you set 3" in UI
      or "believing they set 3" in UI)

print()
print("a duration cannot be misread")
# 120 in a field marked Minutes is two hours. Nothing converted it wrongly --
# it is the number that looks like the two minutes someone may have meant.
check("the field says what it will run for", 'id="ovMinutesSays"' in UI)
check("as it is typed", "says();\n    renderPlanSummary();" in UI)
check("and when a preset or saved script writes the value back",
      "String(Math.round(+d.value / 60));\n                     says(); }" in UI)
_h = fn_body("humanSecs")
check("under a minute is said in seconds", "' seconds'" in _h)
check("an hour or more is said in hours", "' hours'" in _h)
check("and singulars are not said as plurals",
      "' second'" in _h and "' minute'" in _h and "' hour'" in _h)
check("no part of the page states a duration in bare seconds",
      "duration_s+'s'" not in UI and "${t.duration_s||'?'}s" not in UI)
check("those two places use the same wording as the field",
      UI.count("humanSecs(t.duration_s)") == 2)

print()
print("nothing is shown that has no data behind it")
check("no invented noise-category counts",
      "Fonts &amp; CSS ·" not in UI and "Health checks ·" not in UI)
check("no invented review score", "score 92" not in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
