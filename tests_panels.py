"""Stage 2b/2c: phases 2 and 3 split into panels.

Both phases lived in one tall card. The canvas gives each concern its own
panel: the run's progress, the chart, the live calls, the engine log; then the
verdict, the recommendations, the root cause, the Jira ticket.

Three mistakes this file exists to prevent, all of which I made:

  * a panel's phase is INHERITED from the card it came out of, not inferred
    from its heading. "Engine log" matches no phase rule, so a second
    heading-based pass put it back in phase 1 -- correct for one line, wrong by
    the next.
  * panels inserted directly after the source card come out in reverse: the
    engine log sat above the chart it should follow.
  * the split must MOVE nodes. A rebuild loses every id and listener, and loses
    them silently -- the panel looks right and its buttons do nothing.

Run:  ./.venv/bin/python tests_panels.py     # expect FAILURES: 0
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


_split = fn_body("splitIntoPanels")
_tag = fn_body("tagPhases")

print("the phases the canvas names have panels")
for anchor, title in (("liveChart", "Requests, latency and users over time"),
                      ("liveCalls", "Live API calls"),
                      ("liveLog", "Engine log"),
                      ("resRecs", "AI recommendations"),
                      ("resRca", "Root cause & trend"),
                      ("jiraWrap", "Jira ticket")):
    check("%s opens %r" % (anchor, title[:30]),
          "%s:" % anchor in UI and title in UI)

print()
print("panels are anchored on an id, not on a child index")
check("split is keyed by element id", "document.getElementById(anchorId)" in _split)
check("no index arithmetic decides a boundary",
      "children[" not in _split and ".slice(" not in _split)
check("the reason is recorded", "not on a child index" in UI)

print()
print("nodes are moved, never rebuilt")
check("relocated with appendChild", "panel.appendChild(node)" in _split)
check("nothing is cloned", "cloneNode" not in _split)
check("no markup is regenerated for existing content",
      "innerHTML" not in _split)

print()
print("a panel keeps the phase of the card it came from")
check("inherited on creation", "panel.dataset.phase = card.dataset.phase" in _split)
check("and tagPhases leaves it alone",
      "classList.contains('panel')) return" in _tag)
check("so no second pass can re-derive it",
      UI.count("splitIntoPanels();") == 1
      and "tagPhases();          // the new panels" not in UI)

print()
print("panels come out in reading order")
check("each one follows the last", "tail.get(card)" in _split
      and "tail.set(card, panel)" in _split)
check("not stacked against the source card",
      "insertBefore(panel, card.nextElementSibling)" not in _split)

print()
print("a heading a block already carried is reused, not duplicated")
# Headings became eyebrows, so the adopt test widened with them. The point is
# unchanged: a block that already has a heading does not get a second one.
check("an adjacent heading or label is adopted", "/^(H2|LABEL|DIV)$/" in _split)
check("and only an eyebrow counts among divs",
      "classList.contains('eyebrow')" in _split)
check("a panel stops at the next section's heading",
      "node.classList.contains('eyebrow')) break" in _split)
check("a block with its own h2 gets no second one",
      "start.firstElementChild.tagName === 'H2'" in _split)

print()
print("the results split matches the canvas")
check("1.5fr / 1fr", "#resultsGrid{display:grid;grid-template-columns:1.5fr 1fr" in UI)
check("and stacks on a narrow window",
      "@media(max-width:900px){#resultsGrid{grid-template-columns:1fr}" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
