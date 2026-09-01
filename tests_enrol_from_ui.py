"""Fetching card tokens from the APEA page, instead of from a terminal.

The tokens this button fetches are the same values the validation panel beside
it is already checking. Sending someone out to a shell to produce them, then
back to re-upload, is the step most likely to be skipped -- and skipping it is
exactly what makes a card run fail for every user.

What must hold:

  * the endpoint refuses bad input rather than crashing the server
  * it runs OUT of process, because it drives a browser for minutes
  * a card number cannot reach the browser through the log it streams
  * the button only appears when a card is actually wanted AND accounts lack one
  * finishing re-validates the file rather than asking for a re-upload

Run:  ./.venv/bin/python tests_enrol_from_ui.py     # expect FAILURES: 0
"""
import io
import re
import sys

sys.path.insert(0, ".")

FAILURES = []
SRV = io.open("apea/server.py", encoding="utf-8").read()
UI = io.open("apea/static/index.html", encoding="utf-8").read()


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name[:58], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


print("the endpoint exists and is guarded")
check("POST /api/enrol-cards", '@app.post("/api/enrol-cards")' in SRV)
check("GET  /api/enrol-cards/status", '@app.get("/api/enrol-cards/status")' in SRV)
check("a missing CSV is refused, not raised",
      'return {"error": "data CSV not found' in SRV)
check("a missing script is refused too",
      "enrol_cards.py is not present" in SRV)
check("an unknown job id is refused",
      'return {"error": "unknown job"}' in SRV)

print()
print("it runs out of process")
check("a subprocess is used, not an in-process call", "subprocess.Popen" in SRV)
check("on a background thread, so the API stays responsive",
      "threading.Thread" in SRV and "daemon=True" in SRV)
check("the reason is written down", "must stay responsive" in SRV)

print()
print("no card number can reach the browser through the log")
job = SRV.split("def _run():")[1][:1400]
check("the streamed output is redacted", "card redacted" in job)
m = re.search(r're\.sub\(r"([^"]+)"', job)
check("a pattern is compiled from the source, not restated here", bool(m))
if m:
    pat = re.compile(m.group(1))
    # Assert what the pattern must DO, not how it is spelt. The first version of
    # this check pinned the quantifier "13,19"; fixing a real bug in the pattern
    # changed the spelling to "12,18" and broke a test that had not learned
    # anything. Card-number LENGTHS are the contract.
    check("13 digits is a card", bool(pat.fullmatch("4" * 13)))
    check("19 digits is a card", bool(pat.fullmatch("4" * 19)))
    check("12 digits is not", not pat.fullmatch("4" * 12))
    check("20 digits is not a clean match", not pat.fullmatch("4" * 20))
    check("a bare card number is caught",
          pat.sub("X", "token 4111111111111111 saved") == "token X saved")
    check("a spaced card number is caught",
          "4111" not in pat.sub("X", "card 4111 1111 1111 1111"))
    check("an ordinary token is left alone",
          pat.sub("X", "OK b6590e72e0e1") == "OK b6590e72e0e1")
check("the log is bounded, so a long run cannot grow forever",
      'del rec["lines"][:-400]' in SRV)

print()
print("the button appears only when it would help")
panel = UI.split("function enrolPanel()")[1][:1600]
check("nothing without a validated file", "if(!ACCOUNTS) return ''" in panel)
check("nothing when every account already has a token",
      "if(!need.length" in panel)
check("nothing when no card gateway is chosen", "wantsCard" in panel)
check("a per-row method still counts as wanting a card", r"/\{\{/" in panel)

print()
print("finishing closes the loop here, not in a terminal")
# The WHOLE function. Slicing a fixed number of characters passed until the
# function grew past it, then failed on code that was present and correct --
# a test measuring its own window rather than the behaviour.
wire = UI.split("function wireEnrol()")[1].split("\n}\n")[0]
check("it polls rather than streams", "setTimeout(tick" in wire)
check("the reason is written down", "dropped" in wire and "EventSource" in wire)
check("completion re-validates the file", "await validateData()" in wire)
check("it re-validates rather than asking for a re-upload",
      "nothing needs re-uploading" in wire)
check("it says the file on disk is now out of date, and offers the new one",
      "does not have these tokens" in wire and "enrolDl" in wire)
check("a missing target URL is caught before starting",
      "Set the target URL first" in wire)

# --- appended: an invoice payer is not a missing card ------------------------
print()
print("the panel counts card payers, not every empty token cell")
check("the summary separates card logins",
      '"card_logins": card_logins' in SRV)
check("and card logins that lack a token",
      '"card_without_token"' in SRV)
check("a per-row method counts as declaring a card", '"{{" in m' in SRV)
check("the panel prefers that count",
      "ACCOUNTS.card_without_token" in UI)
check("the reason is written down where it was wrong",
      "pay by invoice and need nothing" in SRV
      or "8 pay by invoice" in UI)

# --- appended: a forced method silently overriding the file -------------------
print()
print("the panel names the override instead of blaming the data")
check("the summary reports every method the file declares",
      '"declared_methods": declared_methods' in SRV)
check("and which logins are offline payers", '"offline_logins"' in SRV)
check("and each login's own method", '"method_by_login"' in SRV)
check("the panel only warns when the box forces ONE method",
      "method.indexOf('{{') === -1" in UI)
check("it warns only when the file asks for more than one",
      "declared.length > 1" in UI)
check("it names the rows being overridden", "overridden.map(_escHtml)" in UI)
# The remedy used to be a sentence telling the operator what to type. It is now
# a button that does it. Assert that a remedy is OFFERED, not which form it takes
# -- pinning the sentence made improving it look like a regression.
check("and offers the remedy, as text or as a button",
      "btnUseFileMethod" in UI or "set the payment method to" in UI)

# --- appended: the file decides the payment method ---------------------------
print()
print("uploading the file sets the payment method to match it")
_fn = UI.split("function applyMethodFromData()")[1].split("function applyPayTemplate")[0]
check("several methods in the file -> read it per row",
      "'{{payment_method}}' : declared[0]" in _fn)
check("one method -> use that method, which reads more plainly",
      "declared.length > 1 ?" in _fn)
check("a hand-typed choice is never overwritten",
      "cur !== el.dataset.fromData" in _fn)
check("what it did is explained, not silent", "Set from your data file" in _fn)
check("it runs when the file validates, before the template",
      "applyMethodFromData();     // the file decides the method" in UI)
check("the note element exists to explain it", 'id="ovPayMethodNote"' in UI)

# --- appended: the controls have to read as controls -------------------------
print()
print("the enrolment controls are labelled")
_panel = UI.split("function enrolPanel()")[1].split("function wireEnrol")[0]
# The company field was removed on request -- the data file carries it, and
# both generators now add the column. Its labelling checks went with it; the
# rule they encoded (a control must say what it is) still applies to what is
# left on the panel.
check("the checkbox is labelled", "Show the browser" in _panel)
check("the button says what it does", "Fetch card tokens" in _panel)

# --- appended: the warning has to be actionable, not just correct ------------
print()
print("the override warning carries a button that applies the fix")
check("the button exists in the warning", "btnUseFileMethod" in UI)
check("it sets the box to read the file", "pm.value = '{{payment_method}}'" in UI)
check("it marks the value as ours, so auto-fill keeps working",
      "pm.dataset.fromData = '{{payment_method}}'" in UI)
check("it re-renders so the warning clears itself",
      "applyPayTemplate();\n    renderReadiness();" in UI)
_rr = UI.split("function renderReadiness()")[1].split("\n}")[0]
check("it is wired AFTER the panel is written, not inside the statement",
      _rr.index("el.innerHTML") < _rr.index("btnUseFileMethod\x27"))

# --- appended: the warning has to be actionable, not just correct ------------
print()
print("the override warning carries a button that applies the fix")
check("the button exists in the warning", "btnUseFileMethod" in UI)
check("it sets the box to read the file", "pm.value = '{{payment_method}}'" in UI)
check("it marks the value as ours, so auto-fill keeps working",
      "pm.dataset.fromData = '{{payment_method}}'" in UI)
check("it re-renders so the warning clears itself",
      "applyPayTemplate();\n    renderReadiness();" in UI)
_rr = UI.split("function renderReadiness()")[1].split("\n}")[0]
check("it is wired AFTER the panel is written, not inside the statement",
      _rr.index("el.innerHTML") < _rr.index("btnUseFileMethod\x27"))

# --- appended: the two copies must stop drifting apart ------------------------
print()
print("enrolment hands the updated file back")
check("a download endpoint exists", '@app.get("/api/enrol-cards/csv")' in SRV)
check("the path comes from the job record, never the query string",
      'rec.get("csv")' in SRV and 'src = Path(' in SRV)
check("an unknown job is refused", SRV.count('"unknown job"') >= 2)
check("a vanished file is refused too", "no longer on disk" in SRV)
check("the upload's random prefix is stripped, so it saves over the original",
      'src.name.split("_", 1)' in SRV)
check("the page offers the link when the run finishes", "enrolDl" in UI)
check("and says plainly that the file on disk is now out of date",
      "does not have these tokens" in UI)

# --- appended: the two copies must stop drifting apart ------------------------
print()
print("enrolment hands the updated file back")
check("a download endpoint exists", '@app.get("/api/enrol-cards/csv")' in SRV)
check("the path comes from the job record, never the query string",
      'rec.get("csv")' in SRV and 'src = Path(' in SRV)
check("an unknown job is refused", SRV.count('"unknown job"') >= 2)
check("a vanished file is refused too", "no longer on disk" in SRV)
check("the upload's random prefix is stripped, so it saves over the original",
      'src.name.split("_", 1)' in SRV)
check("the page offers the link when the run finishes", "enrolDl" in UI)
check("and says plainly that the file on disk is now out of date",
      "does not have these tokens" in UI)

# --- appended: three things the operator asked for ---------------------------
print()
print("the panel stays after the tokens arrive")
_p = UI.split("function enrolPanel()")[1].split("function wireEnrol")[0]
check("a success state exists", "Card tokens ready for" in _p)
check("it names the accounts that have one", "have.map(_escHtml)" in _p)
check("it only vanishes when no card is wanted at all",
      "if(!wantsCard) return ''" in _p)
check("the reason is recorded", "Vanishing on success reads as a glitch" in _p)

print()
print("the company field is gone, because the data file carries it")
check("no company input on the page", "enrolCompany" not in UI)
check("a company is still sent", "company: 'APEA Load Test'" in UI)
check("and why is written down", "data file's own column" in UI)

print()
print("'Before you run' sits with the validation it is about")
_i = UI.index('id="validateOut"')
_r = UI.index('id="readiness"')
check("readiness follows the validation result", _r > _i and (_r - _i) < 500)
check("there is only one readiness panel", UI.count('id="readiness"') == 1)

# --- appended: three things the operator asked for ---------------------------
print()
print("the panel stays after the tokens arrive")
_p = UI.split("function enrolPanel()")[1].split("function wireEnrol")[0]
check("a success state exists", "Card tokens ready for" in _p)
check("it names the accounts that have one", "have.map(_escHtml)" in _p)
check("it only vanishes when no card is wanted at all",
      "if(!wantsCard) return ''" in _p)
check("the reason is recorded", "Vanishing on success reads as a glitch" in _p)

print()
print("the company field is gone, because the data file carries it")
check("no company input on the page", "enrolCompany" not in UI)
check("a company is still sent", "company: 'APEA Load Test'" in UI)
check("and why is written down", "data file's own column" in UI)

print()
print("'Before you run' sits with the validation it is about")
_i = UI.index('id="validateOut"')
_r = UI.index('id="readiness"')
check("readiness follows the validation result", _r > _i and (_r - _i) < 500)
check("there is only one readiness panel", UI.count('id="readiness"') == 1)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
