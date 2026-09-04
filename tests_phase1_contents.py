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
import re
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
# The rail's read-out is gone -- it repeated the fields directly above it. The
# sentence survives where it is not a repetition: under the traffic controls.
check("the sentence is said once, by the traffic panel",
      'id="trafficSays"' in UI
      and 'id="railSummary"' not in UI and 'id="railWill"' not in UI)
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
print("the analysis reports itself where the reader is looking")
# It used to run in the rail, under the drop zone, where a seven-step
# checklist had a 308px column to fit into.
check("the progress panel is its own card",
      'id="analysisProgressCard"' in UI and 'id="analysisProgress"' in UI)
check("and it sits above the details it is building",
      UI.index('id="analysisProgressCard"') < UI.index('id="discoveryCard"'))
check("the analysis writes there, not into the rail's file summary",
      "startProgress($('#analysisProgress')" in UI)
check("the rail keeps the uploaded-file summary",
      "uploadFile('recording','upRec','sumRec','recording')" in UI)
check("a finished step is the palette's green, not a literal",
      ".progDone{color:var(--ok)" in UI)
check("and the progress markup carries no colour of its own",
      "startProgress" in UI
      and 'class="progDone"' in UI and 'class="progNow"' in UI
      and "#3ddc97" not in UI[UI.index("function startProgress"):
                              UI.index("async function analyzeRecording")])

print()
print("recording details opens on its own")
check("the section is not collapsed by default",
      '<details class="quiet" open><summary>Recording details' in UI)

print()
print("a tile never cuts its value in half")
# 23px bold in a 130px column clipped the target mid-word.
check("an identifier tile drops to a mono face that fits",
      ".k .v.ident{font-size:14px" in UI and "var(--mono)" in UI)
# The span is gone. On a full-bleed page a quarter of the width is about
# 265px, which the host fits in on one line -- and a tile spanning two columns
# broke the last-row fill rules, which count tiles as cells.
check("the target no longer needs two columns",
      ".k.wide" not in UI and "' wide'" not in UI)
check("the scheme is dropped, since every target has the same one",
      "replace(/^https?:\\/\\//,'')" in UI)
check("but the full URL is still readable on hover",
      'title="${_escHtml(a.base_url)}"' in UI)

print()
print("test data asks the way the canvas asks")
check("the template box states what it will contain, then offers itself",
      'id="dataCols"' in UI and 'id="btnSampleCsv"' in UI
      and "#dataSteps .st > button{margin-top:auto" in UI)
check("the filled CSV is a drop target, not a browse control",
      'id="dropData"' in UI and 'id="btnPickData"' in UI
      and 'id="upData" accept=".csv" class="visually-hidden"' in UI)
check("and it swaps for a summary once there is a file",
      'id="dataUploaded"' in UI and 'id="dataName"' in UI
      and "function syncDataState()" in UI)
check("CHECKED is claimed only once validation passed",
      "!(has && dataValid)" in UI)
check("the credentials CSV is the quieter dashed strip",
      'id="dropUsers"' in UI and 'id="btnPickUsers"' in UI
      and "#dropUsers{border:2px dashed var(--hair)" in UI)
# Three uploads now share one drop-target implementation.
check("one helper wires every drop target",
      "function wireDropTarget(" in UI
      and UI.count("wireDropTarget('#") == 3)
check("the box names the file, so the stats do not repeat it",
      "txt.slice(f.name.length)" in UI)

print()
print("the CSV verdict is stated on the palette, not in literals")
check("a tinted strip with a mono tag",
      ".checkStrip{display:flex" in UI and ".cs-tag{font-family:var(--mono)" in UI)
check("good and blocked take the ok / severity tokens",
      ".checkStrip.good{background:var(--ok-tint);border-left:4px solid var(--ok)}" in UI
      and ".checkStrip.bad{background:var(--sev3-tint);border-left:4px solid var(--sev3)}" in UI)
check("the tints come from the canvas",
      "--ok-tint:rgba(92,214,143,.14)" in UI and "--ok-tint:#E8F3EC" in UI)
# The chart's series moved onto the tokens too, so the last literal went with
# them -- chartColours() resolves them for the canvas instead.
check("no hardcoded verdict greens are left anywhere",
      "#2ecc71" not in UI)
check("and the severity count still matches what the rows carry",
      "rows.filter(r => r.indexOf('var(--sev3)') > -1)" in UI)
# --accent-hi is a dark red; on a #131313 panel the link was barely legible.
check("a link takes the canvas's link colour",
      "a.link{color:var(--eyebrow)" in UI)

print()
print("a column of figures lines up with the word above it")
# The numeric headings were left-aligned while their cells were right-aligned,
# so each heading sat a column's width away from its own numbers.
check("a numeric heading is right-aligned, like its cells",
      "#stageTable th.n{text-align:right}" in UI
      and "#stageTable td.n{text-align:right" in UI)
# A flex row gave the path a different start on every line, because the method
# tag is a different width for every verb.
check("call rows are a grid, not a flex line",
      ".callItem{display:grid;" in UI
      and "grid-template-columns:auto 54px 62px minmax(0,1fr) 96px 48px" in UI)
check("the optional cells are always emitted, empty or not",
      "c.group ? _escHtml(c.group) : ''" in UI
      and "'<span class=\"flag\"></span>'" in UI)
check("the dropped list has columns too",
      ".noiseItem{display:grid;grid-template-columns:54px minmax(0,1fr) 140px" in UI)
check("and the columns fold rather than crush on a narrow window",
      "@media(max-width:820px){" in UI and ".callItem .grp,.callItem .flag{grid-column:4" in UI)

print()
print("a checkbox is not a text field")
# input{width:100%;min-height:44px} was matching checkboxes as well, so the
# tick beside "errors only" rendered as a full-width 44px white block.
check("the field rule excludes ticks, radios and ranges",
      "input:not([type=checkbox]):not([type=radio]):not([type=range]),select{" in UI)
check("and they get a size of their own, in the accent colour",
      "input[type=checkbox],input[type=radio]{width:auto;min-height:0" in UI
      and "accent-color:var(--accent)" in UI)
check("a heading's own controls sit in flow, not floated over it",
      "label.withTools{display:flex" in UI
      and "label.withTools .tools{margin-left:auto" in UI)
check("the tick is still clickable by its words",
      'class="opt" for="callsErrOnly"' in UI)
check("no reference to a token that was never defined",
      "var(--muted)" not in UI)
# There was no base `a` rule at all, so any anchor without .link fell through
# to the browser's default blue -- which is what "Capture fresh tokens instead"
# was rendering as.
check("the design system's own link rule is present",
      "a{color:var(--txt);text-decoration:underline" in UI
      and "a:hover{color:var(--accent)}" in UI)
check("and every inline action link takes the app's link class",
      UI.count('<a href="#" class="link"') == 3
      and '<a href="#" id=' not in UI)

print()
print("the app says when it is working, and says it once")
# These were a 14px spinner and a lowercase word in muted grey -- a caption,
# not the app doing something. One shape now covers upload, validation and the
# test-plan read, on the amber bar because none of them is a verdict.
check("one builder for every busy state", "function _busy(label, what)" in UI)
check("upload, validation and the plan read all use it",
      UI.count("_busy('") >= 3
      and "_busy('Uploading', f.name)" in UI
      and "_busy('Validating'," in UI)
check("no lowercase caption is left in their place",
      "'<span class=\"spin\"></span>uploading" not in UI
      and "<p class=\"muted\">Validating" not in UI)
check("the strip takes the amber bar and a mono caps label",
      ".busy{display:flex" in UI
      and "border-left:4px solid var(--orange)" in UI
      and ".busy .lbl{font-family:var(--mono)" in UI)

print()
print("the analysis reads as a statement, not a caption")
check("the current step is 20px Bold",
      ".progNow{display:flex;align-items:center;gap:12px;font-size:20px" in UI)
check("and it says how far along it is",
      "Step ${n} of ${phases.length}" in UI and ".progStep{" in UI)
check("the finished line keeps the same shape",
      'el.innerHTML = `<div class="progNow">' in UI)

print()
print("a finite list is not cut in half")
# The kept calls are sixteen rows; a 340px cap sliced the last one through the
# middle, which reads as broken rather than scrollable. The live stream keeps
# its cap.
check("the kept-calls list is not capped",
      "#anCalls{max-height:none;overflow:visible}" in UI)
check("but the live stream still is", ".callList{" in UI and "max-height:340px" in UI)

print()
print("tags and type follow one scale")
# The rules, not the file: the hexes are named in a comment explaining why
# they went.
_badges = UI[UI.index(".b-ok{"):UI.index(".b-run{") + 80]
check("a tag takes dark ink, not the old navy or brown",
      "color:var(--tag-ink)" in _badges
      and "#06121f" not in _badges and "#1a1200" not in _badges)
check("a tag is mono caps, like every other tag",
      ".badge{padding:3px 9px;border-radius:0;font-family:var(--mono)" in UI)
# The colour moved out of an inline style and into the row's own class when
# the fields became columns.
check("needs-input takes a ladder colour",
      '.fRow .fv.needs{color:var(--sev1)}' in UI
      and 'class="fv needs">needs input' in UI)
# Those two exceptions are gone now. .kind.XHR and the "skipped" pill each
# paired amber with its own dark background -- which only ever worked on a dark
# page -- and both moved onto the ladder when the chips did.
check("and every amber accent moved",
      "#e6a700" not in UI and "border-left-color:var(--sev1)" in UI)
# 16 distinct sizes, four of them one-offs below the system's caption size.
_sizes = sorted(set(re.findall(r"font-size:([0-9.]+)px", UI)), key=float)
check("no size below the system's 12px caption",
      all(float(x) >= 12 for x in _sizes), "found: %s" % _sizes)
check("and no half-pixel one-offs",
      all("." not in x for x in _sizes), "found: %s" % _sizes)

print()
print("a field's row is columns, so the eye can run down any one of them")
# The badge used to follow the field name, which put it at a different x on
# every line: the name decided where it started.
check("the badge leads the row", '.fRow{display:grid' in UI
      and "grid-template-columns:84px 190px minmax(0,1fr) auto" in UI)
check("and it is emitted first, before the name",
      "return `<div class=\"fRow\">${req}`" in UI)
check("the name is mono, the value muted, the evidence right-aligned",
      ".fRow .fn{font-family:var(--mono)" in UI
      and ".fRow .fv{color:var(--mut)" in UI
      and ".fRow .fe{color:var(--mut);font-size:12px;text-align:right" in UI)
check("needs-input keeps its ladder colour in the new row",
      '.fRow .fv.needs{color:var(--sev1)}' in UI)
check("no <br>-joined flow of text is left",
      "}).join('<br>');" not in UI)
check("and the columns fold on a narrow window",
      "@media(max-width:760px){" in UI)

print()
print("the rail states a thing, then explains it")
check("an option is a short label with its note beneath",
      '<label class="optRow">' in UI and ".optRow{display:flex" in UI)
check("not a paragraph inside a checkbox label",
      "automatically to the plain recording analysis if it isn't)" not in UI
      and "Also crawl live with Playwright</label>" in UI)

print()
print("the checkout timeline uses the app's type, not its own")
# This section had grown a face of its own: an emoji per row, bold sans on
# identifiers that are values, and #8ba0bd on the request line -- a blue-grey
# from a palette the app no longer uses anywhere else.
# Scoped to this renderer. Colour emoji still appear elsewhere in the app --
# a sweep of those is its own decision, not part of cleaning this section.
_tl = fn_body("renderTimeline")
check("no colour emoji is left in the timeline",
      not re.search("[\U0001F000-\U0001FAFF\u2705\u274C]", _tl))
check("a glyph carries the state instead",
      "const ico  = stop ? '\u25a0' : (e.ok ? '\u2713' : '\u2717');" in UI)
check("and the bar beside it comes from the ladder",
      "stop ? 'var(--sev4)' : (e.ok ? 'var(--ok)' : 'var(--sev3)')" in UI)
check("a step row is one weight, with values in mono",
      ".tl-s{font-size:14px;font-weight:var(--fw-bold)}" in UI
      and ".tl-m{font-family:var(--mono)" in UI
      and ".tl-b{font-family:var(--mono)" in UI)
check("the blue-grey on the request line is gone",
      "#8ba0bd\">" not in UI and "color:#8ba0bd" not in UI)

print()
print("the state report is label and value, in columns")
check("two columns, not a run of <br>s",
      ".tlState .r{display:grid;grid-template-columns:170px minmax(0,1fr)" in UI
      and "const row = (l, v) =>" in UI)
check("labels are muted caps, values mono",
      ".tlState .r > .l{color:var(--mut);text-transform:uppercase" in UI
      and ".tlState .r > .v{font-family:var(--mono)" in UI)
check("a mismatch moves the bar rather than shouting in red text",
      ".tlState.mismatch{border-left-color:var(--sev3)}" in UI)
check("the build read-out left the heading for its own line",
      'class="tlMeta" id="timelineMode"' in UI
      and ".tlMeta{font-family:var(--mono)" in UI)

print()
print("nothing is left of the older palettes")
# Every survivor is inside a comment explaining why it went.
for hexv in ("#4f8cff", "#8ba0bd", "#e11627", "#8f9bb3", "#cfe3ff"):
    _live = [ln for ln in UI.split("\n")
             if hexv in ln and not ln.strip().startswith(("/*", "*", "//"))]
    check("no live use of %s" % hexv, not _live,
          "still at: %s" % (_live[:1] or ""))
check("the progress bar is flat, not a gradient",
      ".bar>div{height:100%;background:var(--accent)" in UI)

print()
print("icons come from the canvas's vocabulary")
# 9a's set: U+2B07 for a download, U+25A4 the library, U+25BE/U+25B8 carets,
# U+2039/U+203A the rail, U+2192 flow, U+2197 "opens elsewhere" -- and exactly
# two colour emoji, on two buttons. Those two stay; the rest of the app's
# fourteen either joined the monochrome family or went, because the label
# already says it.
_pict = [c for c in set(UI) if 0x1F000 <= ord(c) <= 0x1FAFF]
check("only the canvas's two colour emoji remain",
      sorted(_pict) == ["\U0001F4BE", "\U0001F4CA"],
      "found: %s" % sorted("U+%04X" % ord(c) for c in _pict))
check("and each is on the button 9a puts it on",
      "\U0001F4CA Open full report" in UI
      and UI.count(">\U0001F4BE Save script<") == 2)
check("the report link is marked as leaving the page, as 9a marks it",
      "\U0001F4CA Open full report \u2197<" in UI)
check("downloads keep the canvas's arrow", UI.count("\u2b07 Download") >= 2)
check("a pictorial button icon is gone, not swapped for another",
      ">Analyze recording<" in UI and ">Crawl &amp; record" in UI)
check("status marks are the monochrome pair",
      "\u2705" not in UI and "\u274C" not in UI and "\u26A0" not in UI
      and "\uFE0F" not in UI)
check("a warning is the geometric triangle", "\u25b2" in UI)

print()
print("nothing is left of any older palette")
_old = ("#5b93ff", "#0f2e4d", "#3a2d12", "#ff6b9a", "#3a1220", "#3ddc97",
        "#123a2a", "#2a2a30", "#c9c9d2", "#3d1b1b", "#ff9a9a", "#e6a700",
        "#8ba0bd", "#4f8cff", "#e11627", "#2ecc71", "#06121f", "#1a1200")
for hexv in _old:
    _live = [ln for ln in UI.split("\n")
             if hexv in ln and not ln.strip().startswith(("/*", "*", "//"))]
    check("no live use of %s" % hexv, not _live, "at: %s" % (_live[:1] or ""))
check("the method chips ride the ladder",
      ".kind.REST{background:var(--ok)}" in UI
      and ".kind.GET{background:var(--sev5)}" in UI
      and "color:var(--tag-ink)}" in UI)
check("and so does the browser-track pill",
      "let color='var(--sev0)', bg='var(--panel2)'" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
