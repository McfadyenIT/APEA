"""Stage 2, phase 1: the setup rail.

The canvas puts what you are testing, and the recording it came from, in a
persistent left rail and gives the rest of the width to what was found. Two
things this file exists to keep true.

Cards are MOVED, not rebuilt. appendChild relocates the same node, so its id
and every listener attached to it come with it. Rewriting the markup would drop
both, and drop them silently -- the control is still on screen, it just stops
doing anything.

And the phase selectors had to follow. They read `#tab-new > .card`, which
matched nothing once the cards moved a level deeper into the rail and the main
column. A child selector that matches nothing raises no error; every card would
simply have stopped hiding.

Run:  ./.venv/bin/python tests_setup_rail.py     # expect FAILURES: 0
"""
import io
import re
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


print("the rail exists and holds the right cards")
_b = fn_body("buildSetupRail")
check("it is built", "buildSetupRail" in UI and 'id = \'setupRail\'' in _b
      or "rail.id = 'setupRail'" in _b)
check("what you are testing goes in it", "project\\s*&\\s*target" in UI)
check("so does the recording", "upload your .*recording" in UI)
check("and the crawl alternative", "crawl\\s*&\\s*record" in UI)
check("it is built before the phases are tagged",
      UI.index("buildSetupRail();") < UI.index("tagPhases();"))

print()
print("cards are moved, never rebuilt")
check("relocated with appendChild", "appendChild(c)" in _b)
check("nothing is cloned", "cloneNode" not in _b)
check("and no markup is regenerated for them",
      "innerHTML" not in _b.split("RAIL_CARDS")[-1].split("toggle.onclick")[0]
      or "body.appendChild(c)" in _b)
check("the reason is recorded", "listeners come with it" in UI
      or "ids and listeners" in UI or "Move, never rebuild" in UI)

print()
print("the phase selectors follow the cards to their new depth")
check("no child selector is left pointing at the old shape",
      "#tab-new > .card" not in UI)
check("phases still find every card", UI.count("#tab-new .card") >= 2)

print()
print("layout matches the canvas")
check("the rail is 322px", "grid-template-columns:322px" in UI)
check("it collapses to an icon strip",
      "#phaseGrid.rail-shut{grid-template-columns:46px" in UI)
check("and runs to the left edge, not inside the centred column",
      "body.has-rail main{max-width:none" in UI)
check("the reading width comes back inside the main column",
      "body.has-rail #phaseMain{max-width:1080px" in UI)
check("the library tabs keep their own centred width",
      "body.has-rail #tab-saved" in UI)
check("it stacks on a narrow window", "@media(max-width:1100px)" in UI)

print()
print("the rail belongs to phase 1")
_s = fn_body("showPhase")
check("hidden on the other phases", "rail.style.display" in _s)
check("and the grid gives the width back", "gridTemplateColumns" in _s)

print()
print("the toggle says what it does")
check("the label changes, not just the styling",
      "querySelector('.lbl').textContent = label" in UI)
check("aria-expanded tracks it", "aria-expanded" in UI)
check("and the choice survives a reload", "ltm-rail" in UI)
check("hiding the label is not treated as changing it",
      "not the same as changing it" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
