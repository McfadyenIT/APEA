"""Wording and read-outs on the API call-groups panel.

An operator asked whether the frozen "19 calls" beside a moving slider was a
bug. It was not -- it counts the requests the recording captured, which no
slider can change. But nothing on the page said so, and nothing showed that the
count and the percentage MULTIPLY, which is the only reason either number
matters. So the panel now names the unit, explains itself, and prints what each
step actually costs per iteration.

Two traps this file exists to catch:

  * the stylesheet uppercases <label>, so a sentence placed in one becomes a
    wall of capitals -- the first attempt did exactly that and read worse than
    the jargon it replaced;
  * the page offered "start from a Performance Test Plan" while that block is
    display:none, naming a route nobody could take.

Run:  ./.venv/bin/python tests_throughput_panel.py     # expect FAILURES: 0
"""
import io
import re
import sys

sys.path.insert(0, ".")

UI = io.open("apea/static/index.html", encoding="utf-8").read()

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


def fn_body(name):
    """One top-level JS function's source. Sliced on the next top-level
    `function` -- brace counting walks off the end on regex literals."""
    start = UI.index("function %s(" % name)
    nxt = UI.find("\nfunction ", start + 1)
    body = UI[start:nxt if nxt > 0 else len(UI)]
    return body[:body.rindex("}") + 1]


def label_block():
    """The <label> for the panel, and the markup up to the panel itself."""
    i = UI.index("What your recording does")
    j = UI.index('<div id="anThroughput"', i)
    return UI[UI.rindex("<label", 0, i + 1):j]


print("the panel says what it is")

blk = label_block()
lab = blk[blk.index(">") + 1:blk.index("</label>")]
check("the label is short enough to be shouted in capitals",
      len(lab.strip()) < 60, "it is %d chars" % len(lab.strip()))
check("the explanation is NOT inside the label",
      "multiply" not in lab and "request count is fixed" not in lab)
check("it lives in a hint paragraph instead",
      'class="hint"' in blk and "multiply" in blk)
check("and hint paragraphs are not uppercased",
      not re.search(r"p\.hint\{[^}]*text-transform", UI))
check("while labels are, which is why it had to move",
      re.search(r"\blabel\{[^}]*text-transform:uppercase", UI) is not None)

print()
print("it names the unit and shows what the slider costs")
check("rows say 'request', not 'call'",
      "${g.count} request${g.count==1?'':'s'}" in UI)
check("each row has a cost read-out", "thrEach" in UI)
check("there is a total line", 'id="thrTotal"' in UI)
check("the cost is computed from count x percent",
      "g.count * pct / 100" in UI)

print()
print("the read-outs cannot go stale")
_hooks = UI.count("renderThroughputCost()")
check("the slider refreshes them", "renderThroughputCost(); };" in UI)
check("so does typing a percentage", _hooks >= 3, "only %d call sites" % _hooks)
check("and they are painted before anything is touched",
      "renderThroughputCost();                    // paint" in UI
      or "// paint the costs before anything is touched" in UI)

print()
print("nothing promises a route the operator cannot take")
_visible = re.sub(r"//[^\n]*", "", UI)          # drop developer comments
_visible = re.sub(r"/\*.*?\*/", "", _visible, flags=re.S)
check("the old JMeter phrasing is gone from the copy",
      "Percent Executions" not in _visible)
check("but is kept in a comment, where it belongs",
      "Percent Executions" in UI)
check("the panel no longer claims a test plan adjusts it",
      "Auto-adjusted from the test plan" not in UI)
check("section 2 no longer offers the hidden test-plan upload",
      "or start from a Performance Test Plan and it fills these in" not in UI)
check("that block really is hidden, which is why the offer had to go",
      "Hidden per request: Performance Test Plan" in UI)

print()
print("a rare checkout is called out")
check("it warns when checkout seldom runs", "1 iteration in" in UI)
check("and when it never runs", "no orders are placed" in UI)


# --------------------------------------------------------------------------
# The total counts the RECORDING, not the run.
#
# The operator noticed the arithmetic did not close: the panel said 40.0 while
# the calls list said "10 of 16 selected". Both were right and neither matched
# what the test sent -- 18.8 requests per lap on their 5-user run.
#
#   40  every step in the recorded journey, across all six groups
#   16  the REST/API subset offered for ticking
#   10  the ones actually ticked
#   18.8  what the generated script sent per lap
#
# The count is honest; "on average per iteration" was not, because it reads as
# a prediction about the run. Say which of the four numbers this is.
# --------------------------------------------------------------------------
print()
print("the total does not pretend to predict the run")
_dl = fn_body("renderThroughputCost")
check("it names the recorded journey",
      "per full pass of the" in _dl and "recorded journey" in _dl)
check("and says the run sends fewer",
      "usually sends fewer" in _dl)
check("crediting both reasons: the ticked calls and correlation",
      "ticked" in _dl and "correlates" in _dl)
check("the misleading phrasing is gone",
      "on average per iteration" not in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
