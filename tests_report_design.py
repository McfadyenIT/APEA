"""The generated HTML report, against the same design as the console.

The report is a separate document built by ltmetrics/reporting.py, and it had
been left behind: the pre-redesign palette (#0b0b0c / #161618 / #9a9aa4), Space
Grotesk for headings, rounded corners throughout, plain underlined <h2> section
headings, and a navy scheme (#1e2b42 / #22304a / #0b1220) from an even older
design. An operator opening a report saw a different product.

Note this covers the TEMPLATE. A report is written to disk when its run
finishes and the route serves that stored file, so reports generated before
this change keep the look they were built with.

Run:  ./.venv/bin/python tests_report_design.py     # expect FAILURES: 0
"""
import io
import re
import sys

sys.path.insert(0, ".")

SRC = io.open("ltmetrics/reporting.py", encoding="utf-8").read()

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


print("the report is on the console's tokens")
check("9a's dark ground, not the old one",
      "--bg:#0A0A0A" in SRC and "--panel:#131313" in SRC
      and "#0b0b0c" not in SRC and "#161618" not in SRC)
check("the severity ladder is available to it",
      "--sev0:#8E93B4" in SRC and "--sev3:#FF7B70" in SRC)
check("and the navy scheme is gone",
      "#1e2b42" not in SRC and "#22304a" not in SRC
      and "#0b1220" not in SRC and "#06121f" not in SRC)

print()
print("typography follows the design system")
check("Aptos leads the stack", "--head:Aptos,Inter," in SRC)
check("no second display face is loaded", "Grotesk:wght" not in SRC)
check("Inter still ships a Light weight", "family=Inter:wght@300;" in SRC)
check("a display heading is Light, a structural one Bold",
      "font-size:30px;font-weight:var(--fw-light)" in SRC
      and "h3{{font-size:20px;font-weight:var(--fw-bold)" in SRC)
check("nothing sets a bare font stack any more",
      "font-family:monospace" not in SRC
      and "font-family:system-ui,Arial" not in SRC)

print()
print("it reads as the same product")
check("a section heading is the canvas's eyebrow",
      "h2{{font-size:12px" in SRC and "letter-spacing:var(--ls-overline)" in SRC)
check("with the node-chain mark before it",
      "h2::before" in SRC and "radial-gradient(circle, var(--mcf-red)" in SRC)
check("and the masthead names the product",
      'class="masthead"' in SRC and "LT METRICS" in SRC)

print()
print("square, like every surface in the console")
_radii = set(re.findall(r"border-radius:\s*([^;\"'}]+)", SRC))
check("no rounded corner survives", not _radii, "found: %s" % sorted(_radii))
check("the gate is a square 44px tag",
      "min-height:44px" in SRC and "font-family:var(--mono)" in SRC)
check("tiles are four-up on a hairline grid",
      "grid-template-columns:repeat(4,1fr);gap:1px" in SRC)
check("a numeric heading aligns with its figures",
      "th.num,td.num{{text-align:right}}" in SRC)

print()
print("colour carries meaning, and only where there is meaning")
# The default was the brand red, so every figure read as an alarm -- including
# "Total Requests".
check("a tile's value is plain text unless told otherwise",
      'def _kpi(label, value, sub="", accent="var(--txt)")' in SRC)
check("priorities ride the severity ladder",
      ".b-P1{{background:var(--sev3)}}" in SRC
      and ".b-P3{{background:var(--sev1)}}" in SRC)
check("and the loose hex literals are gone from the builders",
      "#2ecc71" not in SRC and "#e74c3c" not in SRC and "#8ba0bd" not in SRC
      and "#3ddc97" not in SRC and "#4f8cff" not in SRC)

print()
print("a report gets printed")
check("print takes the light half of the same palette",
      "@media print" in SRC and "--bg:#FFFFFF" in SRC)
check("and does not split a table across a page",
      "break-inside:avoid" in SRC)

print()
print("the charts are handed resolved colours")
# A canvas cannot read a CSS custom property, so these are looked up once.
check("the tokens are resolved for Chart.js",
      "getPropertyValue(n)" in SRC and "C.sev0" in SRC and "C.sev3" in SRC)
check("and no blue-grey scheme is left in the chart config",
      "#26324a" not in SRC and "#e6ecf5" not in SRC)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
