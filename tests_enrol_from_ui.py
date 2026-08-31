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
wire = UI.split("function wireEnrol()")[1][:2400]
check("it polls rather than streams", "setTimeout(tick" in wire)
check("the reason is written down", "dropped" in wire and "EventSource" in wire)
check("completion re-validates the file", "await validateData()" in wire)
check("it does not ask for a re-upload",
      "re-upload" not in wire.lower() or "nothing needs re-uploading" in wire)
check("it says the original file on disk is untouched",
      "the original on disk is unchanged" in wire)
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

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
