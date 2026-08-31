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
import re
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

# --- appended: what the first live run against a real store taught us --------
print()
print("field discovery classifies a real card form")
_HINTS = E._FIELD_HINTS
def _classify(f):
    """Mirror of _discover_card_fields' matching, on one field description."""
    for field, (autos, names, texts) in _HINTS.items():
        hay_name = (f.get("name", "") + " " + f.get("id", "")).lower().replace("-", "_")
        hay_text = (f.get("ph", "") + " " + f.get("aria", "")).lower()
        if (any(a in f.get("auto", "") for a in autos)
                or any(n in hay_name for n in names)
                or any(t in hay_text for t in texts)):
            return field
    return None

check("autocomplete=cc-number is the card number",
      _classify({"auto": "cc-number", "name": "x", "id": "", "ph": "", "aria": ""})
      == "number")
check("a differently NAMED number field is still found by autocomplete",
      _classify({"auto": "cc-number", "name": "accountNumber", "id": "",
                 "ph": "", "aria": ""}) == "number")
check("the documented CyberSource name still matches",
      _classify({"auto": "", "name": "card_number", "id": "", "ph": "", "aria": ""})
      == "number")
check("a placeholder alone is enough",
      _classify({"auto": "", "name": "f1", "id": "", "ph": "Card Number", "aria": ""})
      == "number")
check("security code is not mistaken for the card number",
      _classify({"auto": "cc-csc", "name": "cvn", "id": "", "ph": "", "aria": ""})
      == "cvc")
check("an unrelated field classifies as nothing",
      _classify({"auto": "", "name": "coupon", "id": "", "ph": "Promo code",
                 "aria": ""}) is None)

print()
print("the page-settle helper never waits for the network to go quiet")
src3 = io.open("enrol_cards.py", encoding="utf-8").read()
# Check the CODE, not the prose. _settle's docstring names networkidle in order
# to explain why it is wrong, so a plain substring test fails on its own
# explanation -- which is what happened the first time this was written.
check("nothing waits on networkidle anywhere",
      'wait_for_load_state("networkidle"' not in src3
      and "wait_for_load_state('networkidle'" not in src3)
check("_settle waits for the document instead",
      'wait_for_load_state("domcontentloaded"' in src3)

print()
print("the product comes from the data file")
# Corrected after probing the live store. product_id through Magento's CORE
# route was removed on the assumption it would not work, because the store's
# pretty URLs are /buy/<slug>/<id>.html. The store serves BOTH -- the core
# route returns the product page with an add button. Direct beats search: one
# navigation, no result list to parse, nothing to click.
check("every product column in the data file is used",
      all(c in src3 for c in ("product_id", "search_keyword", "sku")))
# Strip comments and docstrings first. This check failed twice on prose: the
# code comment that EXPLAINS the /buy/<slug>/<id>.html shape contains it, and a
# plain substring test cannot tell an explanation from an instruction.
_code_only = re.sub(r'"""[\s\S]*?"""', "", src3)
_code_only = "\n".join(l for l in _code_only.split("\n")
                       if not l.lstrip().startswith("#"))
check("the store's pretty-URL shape is never invented",
      "/buy/" not in _code_only)
check("search remains, so a store with no usable id still works",
      "catalogsearch/result" in src3)

# --- appended: what the second live run taught us ----------------------------
print()
print("the second live run's causes are addressed")
src4 = io.open("enrol_cards.py", encoding="utf-8").read()
check("consent banners are dismissed before clicking",
      "_dismiss_overlays" in src4 and "onetrust" in src4.lower())
check("decline is preferred over accept",
      src4.index("reject-all-handler") < src4.index("accept-btn-handler"))
check("clicks wait for the element instead of asking count()",
      "_click_when_ready" in src4 and 'wait_for(state="visible"' in src4)
check("the direct product route is tried first",
      "product_url" in src4 and "/catalog/product/view/id/" in src4)
check("search is still there as the store-agnostic fallback",
      "catalogsearch/result" in src4)
check("each attempt's own reason is reported, not one lumped message",
      'notes.append' in src4)

# --- appended: the empty-iframe run ------------------------------------------
print()
print("the gateway frame is chosen by CONTENT, not by URL alone")
src5 = io.open("enrol_cards.py", encoding="utf-8").read()
check("child frames are searched too",
      "child_frames" in src5)
check("a frame only counts once it holds real inputs",
      'input:not([type=hidden])' in src5)
check("a timed-out search still returns what it saw, for the report",
      "best = best or f" in src5 and "return best" in src5)

print()
print("checkout is walked to the payment step before the form is expected")
check("the delivery step is advanced", "_reach_payment_step" in src5
      and "ship_method" in src5)
check("the card method is selected, because the form renders on selection",
      "was not selectable" in src5)
check("the method comes from the row, so a mixed pool still works",
      'row.get("payment_method")' in src5)
check("a failure names the frames that WERE present",
      "the page holds these frames" in src5)

# --- appended: the address form that stopped every run -----------------------
print()
print("the delivery address is typed from the data file")
src6 = io.open("enrol_cards.py", encoding="utf-8").read()
cols = [c for c, _ in E._ADDRESS_MAP]
for col in ("firstname", "lastname", "street", "city", "postcode", "telephone"):
    check("%s comes from the row" % col, col in cols)
check("the email field is filled from username", "username" in cols)
check("country is a dropdown, so select_option is used", "select_option" in src6)
check("county handles both a list and a free text box",
      "select[name='region_id']" in src6 and "input[name='region']" in src6)
check("the row reaches the address step",
      'cfg["row"] = row' in src6 and 'cfg["row"]' in src6)
check("what was filled is reported back",
      "address: %s" in src6)

# --- appended: the two-column address form and the Place Order button --------
print()
print("a form that asks the same thing twice gets both filled")
src7 = io.open("enrol_cards.py", encoding="utf-8").read()
check("_fill_all exists and iterates every match",
      "async def _fill_all" in src7 and "loc.nth(i)" in src7)
check("the address step uses it, not the first-match helper",
      "n = await _fill_all(page, sel, val)" in src7)
check("company is in the address map", "company" in [c for c, _ in E._ADDRESS_MAP])
check("a company default exists for files with no such column",
      '"--company"' in src7)

print()
print("buttons are found by the words on them")
for label in ("Place Order", "Add to Cart", "Next"):
    check("%r is matched by text" % label, "has-text('%s')" % label in src7)
check("a missing Place Order reports the buttons that WERE on screen",
      "Visible buttons:" in src7)

# --- appended: the address form is drawn late --------------------------------
print()
print("the address form is waited for, then reported if it does not match")
src8 = io.open("enrol_cards.py", encoding="utf-8").read()
check("it waits for an address field to be visible before typing",
      'wait_for(state="visible", timeout=40000)' in src8)
check("filling nothing reports the form's REAL field names",
      "NOTHING FILLED" in src8)
check("the address outcome reaches the card-form failure too",
      "Earlier: %s" in src8)

# --- appended: our own litter broke the checkout ------------------------------
print()
print("the basket is emptied before each attempt")
src9 = io.open("enrol_cards.py", encoding="utf-8").read()
check("an empty-cart step exists", "async def _empty_cart" in src9)
check("it runs before the product is added",
      src9.index("await _empty_cart(page, cfg)") < src9.index("attempts, tried = [], []"))
check("the removal loop is bounded, not while-true",
      "for _ in range(40)" in src9)
check("a checkout stuck on a spinner says so, and says why",
      "CHECKOUT NEVER FINISHED LOADING" in src9)

# --- appended: a gateway this cannot enrol must SAY so -----------------------
print()
print("rows are classified into enrol / skip / cannot")
KB2 = {"cybersource": SANDBOX}
HOSTED2 = ["cybersource", "paradoxlabs", "stripe", "paypal"]

def _act(row):
    return E._classify_row(row, HOSTED2, KB2)[0]

check("a card row with no token is enrolled",
      _act({"payment_method": "paradoxlabs_cybersource", "payment_token": ""})
      == "enrol")
check("a row that already has a token is skipped",
      _act({"payment_method": "paradoxlabs_cybersource", "payment_token": "abc"})
      == "skip")
check("net terms is skipped, it needs nothing",
      _act({"payment_method": "netterms", "payment_token": ""}) == "skip")
check("PayPal is reported, NOT silently skipped",
      _act({"payment_method": "paypal_express", "payment_token": ""}) == "cannot")
check("Klarna likewise", _act({"payment_method": "klarna", "payment_token": ""})
      == "cannot")
check("a gateway with no KB profile is reported, not attempted",
      _act({"payment_method": "worldpay_hosted", "payment_token": ""}) == "skip")

reason = E._classify_row({"payment_method": "paypal_express", "payment_token": ""},
                         HOSTED2, KB2)[1]
check("the PayPal reason explains why, and what to do instead",
      "sign in" in reason and "offline" in reason)

src10 = io.open("enrol_cards.py", encoding="utf-8").read()
check("skipped gateways are printed, not swallowed", "SKIPPED" in src10)
check("a run that enrolled nothing but skipped something exits non-zero",
      "return 0 if not cannot else 1" in src10)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
