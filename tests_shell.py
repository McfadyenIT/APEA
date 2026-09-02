"""The LT Metrics shell: header, four phases, light/dark.

Stage 1 of the approved redesign. The rule it has to keep is that nothing
moves: a card the current phase does not own is hidden, never removed, so all
140 elements the page addresses by id are still there for their handlers. Break
that and the app fails silently -- the button is gone, no error is raised.

Two traps this file exists to catch:

  * showPhase() edits `phase-off` on every card, which fires the same observer
    that watches for the app revealing a card. Without a guard it switched
    phase, saw the cards it had just hidden, and switched straight back.
  * the app reveals the live-run and results cards itself by dropping `hidden`.
    If the phase system ignores that, starting a run looks like nothing
    happened.

Run:  ./.venv/bin/python tests_shell.py     # expect FAILURES: 0
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


print("the approved palette, not an approximation")
for token, value in (("--green", "#23674A"), ("--orange", "#EF9253"),
                     ("--peach", "#FBD9BE"), ("--accent", "#C91A12"),
                     ("--txt", "#191919"), ("--mut", "#767676"),
                     ("--ok", "#0E8A4F")):
    check("%s is %s" % (token, value), "%s:%s" % (token, value) in UI)

check("the header is a flat green bar",
      "background:var(--green)" in UI)
# This was always about the HEADER's radial-gradient, not gradients in
# general -- 9a draws its eyebrow marker from them.
check("no gradient survives on the header",
      "radial-gradient(120% 180%" not in UI)
check("but the eyebrow marker uses them, as 9a does",
      "radial-gradient(circle, var(--mcf-red)" in UI)

print()
print("the mark is red; the words are not")
# 9a sets these separately -- o11Eyebrow is #B34E14 light / #EF9253 dark while
# the mark stays #E00000. One colour for both made the overline read hot.
check("the eyebrow has its own colour token", "--eyebrow:#B34E14" in UI)
check("which is not the mark's red",
      "--eyebrow:#E00000" not in UI and "--mcf-red:#E00000" in UI)
check("and it changes in dark, as 9a does", "--eyebrow:#EF9253" in UI)
check("the words take that token, not the mark's",
      "letter-spacing:var(--ls-overline);\n    color:var(--eyebrow)" in UI)
check("the mark itself stays red",
      "background:\n      radial-gradient(circle, var(--mcf-red)" in UI)

print()
print("a section that happens to be collapsed is still a section")
check("summaries share the eyebrow's type",
      ".eyebrow,.railEyebrow,details.quiet > summary{" in UI)
check("and its mark",
      ".railEyebrow::before,details.quiet > summary::before{" in UI)
check("they no longer set a colour of their own",
      "details.quiet > summary{cursor:pointer;list-style:none}" in UI)
check("the open/closed arrow moved out of the mark's place",
      "details.quiet > summary::after{content:" in UI)
check("so it cannot displace the mark",
      "details.quiet > summary::before{content:" not in UI)
check("the recorded journey is one as well",
      '<div class="eyebrow">Recorded journey</div>' in UI)
check("the kept-calls heading is a section heading too",
      '<div class="eyebrow" style="margin-top:18px">What we kept' in UI)

print()
print("light by default, and the old palette is kept rather than deleted")
check("dark is a theme, not the default",
      'html[data-theme="dark"]' in UI)
check("the previous dark values are still there",
      "--bg:#0b0b0c" in UI and "--txt:#f4f4f6" in UI)
check("the toggle is wired", "btnTheme" in UI and "applyTheme" in UI)
check("and the choice survives a reload", "ltm-theme" in UI)

print()
print("four phases, built from cards that already exist")
check("the bar is there", 'id="phases"' in UI)
for n, label in ((1, "Set up the test"), (2, "Watch it run"),
                 (3, "Results"), (4, "Understand the result")):
    check("phase %d is %r" % (n, label), label in UI)
check("phases are announced to assistive tech",
      'role="tablist"' in UI and 'aria-selected' in UI)

_tag = fn_body("tagPhases")
check("a card is placed by what it declares, not by what it says",
      "PHASE_DECLARED.has(c) ? PHASE_DECLARED.get(c) : '1'" in _tag)
check("and nothing is inferred from heading text any more",
      "PHASE_RULES" not in UI)
check("an undeclared card falls to phase 1 rather than vanishing",
      ": '1'" in _tag)

_show = fn_body("showPhase")
check("a card is hidden, never removed",
      "classList.toggle('phase-off'" in _show and "remove()" not in _show)
check("the CSS only hides it", ".card[data-phase].phase-off{display:none}" in UI)

print()
print("the phase system follows the app instead of fighting it")
_w = fn_body("watchPhaseReveals")
check("it watches for a card being revealed", "MutationObserver" in _w)
check("only the moment `hidden` goes away counts",
      "before === true" in _w and "nowHidden === false" in _w)
check("its own edits are ignored", "PHASE_MUTING" in _w)
check("and showPhase raises that guard", "PHASE_MUTING = true" in _show)
check("released after the batch drains", "PHASE_MUTING = false" in _show)

print()
print("a card names its own phase; the heading text does not decide")
# The regression this exists for: rewording #liveCard's heading to 9a's
# "Running" removed the words /live execution/ the rule matched on, so the
# card and its three panels fell to the default phase 1 and "Watch it run"
# rendered blank.
check("the live-run card is phase 2 in the markup",
      'id="liveCard" data-phase="2"' in UI)
check("so is auto-heal", 'id="healCard" data-phase="2"' in UI)
check("results are phase 3", 'id="resultCard" data-phase="3"' in UI)
check("the stage breakdown is phase 4", 'id="stageCard" data-phase="4"' in UI)
check("and the timeline with it", 'id="timelineCard" data-phase="4"' in UI)
check("the declaration is captured before anything overwrites it",
      "PHASE_DECLARED = new Map()" in UI)
check("and that declaration is what tagPhases assigns",
      "c.dataset.phase = PHASE_DECLARED.has(c)" in UI)
check("the wording that was replaced is not referred to anywhere",
      "live execution" not in UI)

print()
print("a phase with nothing to show says so")
_empty = fn_body("showPhaseEmptyState")
check("each of 2, 3 and 4 has something to say",
      all(("'%s':" % n) in UI for n in ("2", "3", "4")) and "PHASE_EMPTY" in UI)
check("phase 1 is never empty, so it gets no notice",
      "PHASE_EMPTY = {\n  '2'" in UI)
check("it is said only when nothing is visible", "anyVisible" in _empty)
check("hidden cards do not count as content",
      "classList.contains('hidden')" in _empty)
check("and it is removed once there is content",
      "if(anyVisible || !copy){ if(box) box.remove(); return; }" in _empty)
check("showPhase refreshes it", "showPhaseEmptyState(n)" in fn_body("showPhase"))
check("so does a reveal, even mid-run",
      "showPhaseEmptyState(CURRENT_PHASE)" in UI)
check("checked before the run guard, not after",
      UI.index("showPhaseEmptyState(CURRENT_PHASE)")
      < UI.index("if(RUN_IN_FLIGHT) return;"))

print()
print("the phases belong to the run, not to the library tabs")
check("they are hidden on the other tabs",
      "b.dataset.tab === 'new'" in UI and "ph.style.display" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
