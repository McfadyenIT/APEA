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
                     ("--txt", "#191919"), ("--mut", "#5A4A3F"),
                     ("--ok", "#157347")):
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
print("dark by default, with light as the other half of the same palette")
check("the default is set before the first paint, not by script",
      '<html lang="en" data-theme="dark"' in UI)
check("and the script agrees with the markup, fallback included",
      "let t = 'dark'" in UI
      and "getItem('ltm-theme') || 'dark'" in UI)
check("9a's dark values are the ones used",
      "--bg:#0A0A0A" in UI and "--txt:#F4F4F4" in UI
      and "--panel:#131313" in UI)
check("the light half is warm, not bleached",
      "--panel:#FBEEE3" in UI and "--line:#B49A82" in UI)
check("the toggle is wired", "btnTheme" in UI and "applyTheme" in UI)
check("and the choice survives a reload", "ltm-theme" in UI)
check("the toggle offers the other theme, not the current one",
      ">Light mode</button>" in UI)

print()
print("the doodle backdrop reads only through the page surface")
check("it is one repeated 300px tile", "background-size:300px 300px" in UI)
check("behind every in-flow element", "body::before" in UI and "z-index:-1" in UI)
check("it cannot swallow a click", "pointer-events:none" in UI)
check("dark inverts the strokes, light leaves them dark",
      "--doodle-invert:invert(1)" in UI and "--doodle-invert:none" in UI)
# A DELIBERATE DIVERGENCE from the canvas. 9a sets .07 dark / .05 light over
# opaque cards, where the pattern only ever shows in the gaps between them. The
# sections are half-transparent now, so at 9a's numbers the doodle landed at
# about 3% through a card and read as dirt rather than pattern. Lifted one step.
check("the backdrop is lifted for half-transparent sections",
      "--doodle-op:.10" in UI and "--doodle-op:.07" in UI)

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
print("nothing keeps a colour from the palette it was written for")
# .callItem:hover was #26262b, a dark-palette value left in place, so hovering
# a call under the light theme turned the row black with dark text on it.
check("the call hover uses the theme, not a fixed colour",
      ".callItem:hover{background:var(--panel2)}" in UI)
check("and that colour is defined for both themes",
      UI.count("--panel2:") >= 2)
check("no hover hardcodes a dark-palette background",
      "#26262b" not in UI)

print()
print("the chart is drawn in whatever theme is showing")
_cc = fn_body("chartColours")
check("its colours come from the theme's tokens",
      "--line" in _cc and "--mut" in _cc)
check("with a fallback if a token is missing", "|| f" in _cc)
# This reverses an earlier choice. The bars were moved to --peach before the
# canvas was read; 9a assigns the request bars o11Sev0, p95 o11Sev3 and the
# users line o11Muted, so the series follow the canvas now.
check("the three series take the canvas's colours",
      "reqs: tok('--sev0'" in _cc and "p95: tok('--sev3'" in _cc)
check("and the chart reads them as literals, since a canvas cannot",
      "borderColor:_cc.reqs" in UI and "borderColor:_cc.p95" in UI
      and "borderColor:_cc.mut" in UI)
check("the gate is drawn, but not named twice",
      "borderDash:[4,4]" in UI and "filter:i=>i.text!=='p95 SLA'" in UI)
_at = fn_body("applyChartTheme")
check("a theme switch recolours what is already drawn",
      "liveChart.update('none')" in _at)
check("rather than rebuilding and losing the run so far",
      "destroy()" not in _at)
# applyTheme() also runs during start-up, hundreds of lines above
# `let liveChart = null`. Touching the chart there threw before it existed and
# aborted the rest of the start-up script.
_apply = UI[UI.index("function applyTheme(t){"):]
_apply = _apply[:_apply.index("\n}") + 2]
check("start-up's applyTheme does not reach for the chart",
      "applyChartTheme" not in _apply)
check("the switch does it from the click instead",
      "applyTheme(document.documentElement.getAttribute('data-theme') === 'dark'"
      in UI and "applyChartTheme();\n  };" in UI)

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
print("the design system's typography, not an approximation of it")
# AIBP Design System V2.0: --font-sans leads with Aptos, --font-display is the
# same stack, and only weight separates a statement heading from a structural
# one. The app used to lead with Inter and set every heading to 700.
check("Aptos leads both stacks",
      UI.count("--head:Aptos,Inter,") == 1 and UI.count("--body:Aptos,Inter,") == 1)
check("and the mono stack is the system's",
      "--mono:'JetBrains Mono','SF Mono',Consolas" in UI)
check("no second display face is loaded", "Grotesk:wght" not in UI)
check("Inter still ships a Light weight for the headings",
      "family=Inter:wght@300;" in UI)
check("a statement heading is 30px Light",
      "font-size:30px;font-weight:var(--fw-light)" in UI)
check("a card title stays Bold", "font-weight:var(--fw-bold)" in UI)
check("nothing sets its own font stack any more",
      "font-family:ui-monospace" not in UI
      and "font-family:monospace" not in UI)

print()
print("square, 44px controls -- 9a draws no rounded corner at all")
check("one control height, from a token", "--ctl-h:44px" in UI)
check("inputs and buttons both take it",
      UI.count("min-height:var(--ctl-h)") >= 2)
# Every radius in the file is 0; the only survivor is the spinner, which is a
# circle rather than a corner treatment.
_radii = set(re.findall(r"border-radius:\s*([^;\"'}]+)", UI))
check("and every corner is square", _radii <= {"0", "50%"},
      "found: %s" % sorted(_radii))
check("a checkbox label is a sentence, not a field name",
      "label:has(> input[type=checkbox])" in UI)

print()
print("a tile row always fills, so no cell shows as a blank slab")
# Ten tiles in four columns left two cells empty, and an empty cell shows the
# grid's own hairline background -- which read as a broken panel.
check("the last row's tiles span what is left",
      ".grid-k > .k:last-child:nth-child(4n+1){grid-column:span 4}" in UI
      and ".grid-k > .k:last-child:nth-child(4n+2)," in UI)
check("the arithmetic is not broken by a tile asking for two columns",
      ".k.wide{grid-column:span 2}" not in UI and "' wide'" not in UI)
check("and it re-fills at each breakpoint",
      "@media(max-width:900px){\n    .grid-k > .k:last-child:nth-child(2n+1)" in UI)

print()
print("the run says its state once")
check("the badge and the sentence share one row",
      'id="liveState"' in UI and "#liveState{display:flex" in UI)
check("the sentence is prominent, not a muted hint",
      "#livePhase{margin:0;font-size:16px;color:var(--txt)" in UI
      and '<p class="hint" id="livePhase">' not in UI)
# The server's phase for a finished run is "Done", which is what the COMPLETED
# badge beside it already says.
check("a phase that only echoes the badge is not shown",
      "const _echoes={completed:['done','complete','completed','finished']" in UI
      and "(_echo?'':_phase)" in UI)
check("but a real message still is", "s.error?((!_echo&&_phase?' \u2014 ':'')+s.error)" in UI)
check("and the placeholder heading no longer echoes it either",
      '>Starting the run\u2026</h2>' in UI
      or '>Starting the run' in UI and 'id="liveSays"' in UI)

print()
print("the library is a drawer, not a set of sibling tabs")
# 9a has no tab strip: the run is the page, and the library and the question
# box are pulled over it. The phases never need hiding, because you never
# navigate away from the run to reach either one.
check("there is no tab strip left", "<nav>" not in UI and "data-tab=" not in UI)
check("and nothing still reaches for one", "nav button" not in UI)
check("the drawer is a modal dialog over a scrim",
      'id="dockScrim"' in UI
      and 'role="dialog"' in UI and 'aria-modal="true"' in UI)
check("560px, docked right, as the canvas has it",
      "#dockPanel{position:fixed" in UI and "width:560px" in UI)
check("both views drive the one drawer",
      "library:" in UI and "ask:" in UI and UI.count("function openDock") == 1)
check("the four sections moved into it rather than being rebuilt",
      all(('id="tab-%s"' % t) in UI for t in ("saved", "history", "projects", "ask")))
check("escape closes it", "e.key === 'Escape'" in UI)
check("and the page underneath stops scrolling while it is open",
      "document.body.style.overflow = 'hidden'" in UI)

print()
print("a library row is one line: a name and a single value")
check("no tables survive in the drawer", "<tbody" not in UI)
check("rows are the canvas's flex line", ".dockList > *{display:flex" in UI)
check("the left bar carries the state, on 9a's severity ladder",
      "--sev0:#8E93B4" in UI and "--sev3:#FF7B70" in UI)
check("the script already loaded says so instead of offering itself",
      ">in use<" in UI)

print()
print("the instrument panel and the plot, as the canvas draws them")
# The canvas sets the tile rows four-up and separates them with a 1px rule
# rather than a gap, so a row reads as one panel instead of four cards.
check("tiles are four-up on a hairline grid",
      ".grid-k{display:grid;grid-template-columns:repeat(4,1fr);gap:1px" in UI
      and "background:var(--hair);border:1px solid var(--hair)}" in UI)
check("so a tile draws no border of its own",
      ".k{background:var(--panel2-a);border:none" in UI)
# "Reduce the opacity of each section so the doodle shows across the app."
# Applied to the background, not to the element: opacity on the element would
# have faded every figure and label inside it too.
check("sections are translucent by background, not by opacity",
      ".card{background:var(--panel-a)" in UI
      and "--panel-a:rgba(19,19,19,.5)" in UI
      and "--panel-a:rgba(251,238,227,.5)" in UI)
# A disabled control is allowed to fade, text and all -- that is what disabled
# looks like. A section is not: it would take its own figures down with it.
_sections = re.findall(r"\.(card|k|st|thrList|chartBox)[^{]*\{([^}]*)\}", UI)
check("and no section fades its own text",
      not any("opacity" in body for _, body in _sections))
check("but keeps its severity rule on top",
      "#liveKpis .k,#resKpis .k{border-top:3px solid var(--line)" in UI)
check("and it folds down rather than shrinking to nothing",
      "@media(max-width:900px){.grid-k{grid-template-columns:repeat(2,1fr)}}" in UI)
check("the plot sits in the canvas's box",
      "#chartBox{border:1px solid var(--hair);background:var(--panel2)" in UI
      and "padding:18px 18px 12px" in UI)
check("with the switch on the eyebrow's own row",
      'id="chartHead" class="eyebrow"' in UI and "margin-left:auto}" in UI)
check("which is why the split adopts it instead of adding a second heading",
      "stay an" in UI and "splitIntoPanels" in UI)
check("and closes with a mono read-out naming each axis",
      'id="chartFoot"' in UI and "req/s left axis" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
