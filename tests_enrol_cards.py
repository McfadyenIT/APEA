"""Guards on the card-enrolment script.

The browser flow itself cannot be tested here -- it needs a real store and a
real card form. What CAN and MUST be tested is everything that decides whether a
browser opens at all, because those are the checks that keep an unattended
script from doing something it should not:

  * it never runs against a production gateway host
  * it never uses a card that is not a published sandbox test card
  * it only touches accounts that DECLARED a card and lack a token
  * no card number can reach a log line or an error message

Run:  ./.venv/bin/python tests_enrol_cards.py     # expect FAILURES: 0
"""
import io
import sys

sys.path.insert(0, ".")

import enrol_cards as E  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


def expect_exit(name, fn, needle):
    try:
        fn()
    except SystemExit as exc:
        check(name, needle.lower() in str(exc).lower(), "said: %s" % exc)
        return
    check(name, False, "it did NOT refuse")


SANDBOX = {
    "safeguards": {"real_pan_prohibited": True,
                   "allowed_submit_hosts": ["testsecureacceptance.cybersource.com"],
                   "forbid_submit_hosts": ["secureacceptance.cybersource.com"]},
    "test_cards": {"success": {"number": "4111111111111111", "exp": "12 / 34",
                               "cvc": "123", "postal": "12345"}},
    "frame_url_patterns": ["testsecureacceptance.cybersource.com"],
    "card_element_fields": {"number": "input[name='card_number']"},
}

print("host rules match on label boundaries, not naive substring")
# The bug this pins: the sandbox host CONTAINS the production host as a
# substring, so `in` refuses the one host the script is meant to use.
check("sandbox is NOT mistaken for production",
      not E._host_matches("testsecureacceptance.cybersource.com",
                          "secureacceptance.cybersource.com"))
check("production matches itself",
      E._host_matches("secureacceptance.cybersource.com",
                      "secureacceptance.cybersource.com"))
check("a real subdomain still matches",
      E._host_matches("eu.secureacceptance.cybersource.com",
                      "secureacceptance.cybersource.com"))
check("an unrelated host does not match",
      not E._host_matches("cybersource.com.evil.test",
                          "secureacceptance.cybersource.com"))

print()
print("the card is never typed into a production form")
G = SANDBOX["safeguards"]
expect_exit("a production card form is refused",
            lambda: E._assert_frame_allowed(
                "https://secureacceptance.cybersource.com/embedded/pay", G),
            "production")
expect_exit("a form on some other host is refused",
            lambda: E._assert_frame_allowed("https://pay.example.com/form", G),
            "not in the knowledge")
E._assert_frame_allowed("https://testsecureacceptance.cybersource.com/embedded/pay", G)
check("the sandbox form is allowed", True)

print()
print("the sandbox test card is what gets used")
check("the KB sandbox card is returned",
      E._check_safeguards(SANDBOX, "https://mcstaging.radwell.eu/uk",
                          False)["number"] == "4111111111111111")

print()
print("it refuses to ask for a real card")
no_card = {"safeguards": {"real_pan_prohibited": True}, "test_cards": {}}
expect_exit("no sandbox card in the KB -> refuse rather than prompt",
            lambda: E._check_safeguards(no_card, "https://store.example", False),
            "refusing")

print()
print("no card number can leak into a message")
check("a bare PAN is redacted",
      "4111111111111111" not in E._redact("failed with card 4111111111111111"))
check("a spaced PAN is redacted",
      "4111" not in E._redact("card 4111 1111 1111 1111 declined"))
check("a dashed PAN is redacted",
      "4242" not in E._redact("card 4242-4242-4242-4242 declined"))
check("ordinary text survives", "no card field matched"
      in E._redact("no card field matched input#number"))

print()
print("it only touches accounts that need enrolling")
HOSTED = ["cybersource", "paradoxlabs", "stripe"]
check("card row with no token -> enrol",
      E._needs_token({"payment_method": "paradoxlabs_cybersource",
                      "payment_token": ""}, HOSTED))
check("card row that already has a token -> skip",
      not E._needs_token({"payment_method": "paradoxlabs_cybersource",
                          "payment_token": "f97d08cf"}, HOSTED))
check("net-terms row -> skip, it needs no card",
      not E._needs_token({"payment_method": "netterms", "payment_token": ""}, HOSTED))
check("wire payment row -> skip",
      not E._needs_token({"payment_method": "wirepayment", "payment_token": ""}, HOSTED))
check("blank method -> skip, nothing was declared",
      not E._needs_token({"payment_method": "", "payment_token": ""}, HOSTED))
check("whitespace-only token counts as absent",
      E._needs_token({"payment_method": "stripe_payments",
                      "payment_token": "   "}, HOSTED))

print()
print("it reads the token the way the store actually sends it")
BODY = ('{"paymentMethod":{"method":"paradoxlabs_cybersource","additional_data":'
        '{"card_id":"9978b2051325671b1547a509a38040b3e8484c89","cc_cid":"123",'
        '"save":false}}}')
m = E._TOKEN_RE.search(BODY)
check("the 40-character token is extracted",
      bool(m) and m.group(1) == "9978b2051325671b1547a509a38040b3e8484c89")
check("a body with no token yields nothing",
      E._TOKEN_RE.search('{"paymentMethod":{"method":"netterms"}}') is None)

print()
print("gateway profiles resolve by substring, as elsewhere in APEA")
KB = {"cybersource": SANDBOX, "stripe": {"frame_url_patterns": ["js.stripe.com"]}}
check("paradoxlabs_cybersource finds the cybersource profile",
      E._gateway_profile(KB, "paradoxlabs_cybersource") is SANDBOX)
check("an unknown gateway yields nothing rather than a wrong profile",
      E._gateway_profile(KB, "worldpay_hosted") == {})

print()
print("the CSV never carries card details")
src = io.open("enrol_cards.py", encoding="utf-8").read()
for bad in ('row["card_number"]', 'row.get("card_number")', '"card_number":'):
    check("does not read a card number from the data file (%s)" % bad, bad not in src)
check("the card comes from the knowledge base", "_check_safeguards" in src
      and "test_cards" in src)
check("the original file is backed up before it is rewritten",
      "shutil.copyfile" in src)

# --- appended: choosing among the gateway's published cards ------------------
print()
print("the test card can be chosen, but only from the knowledge base")
MULTI = dict(SANDBOX)
MULTI["test_cards"] = {
    "success": {"number": "4111111111111111", "cvc": "123"},
    "decline": {"number": "4000000000000002", "cvc": "123"},
}
check("the default is the success card",
      E._check_safeguards(MULTI, "https://s.example", False)["number"]
      == "4111111111111111")
check("a named card is honoured",
      E._check_safeguards(MULTI, "https://s.example", False, "decline")["number"]
      == "4000000000000002")
expect_exit("an unknown card name is refused, and lists what exists",
            lambda: E._check_safeguards(MULTI, "https://s.example", False, "nosuch"),
            "available")
src2 = io.open("enrol_cards.py", encoding="utf-8").read()
check("there is no way to pass a raw card number on the command line",
      "--card-number" not in src2 and "card_number=" not in src2)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
