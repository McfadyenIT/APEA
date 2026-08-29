"""One pool, mixed payment methods.

APEA used to force ONE payment method on every virtual user. That is wrong for
the store it was built against: the checkout offers paradoxlabs_cybersource,
paypal_express, netterms and wirepayment, and on a B2B distributor most real
orders are placed on account, not by card. A pool where all ten users pay by
card measures a population the site does not have.

It also mattered practically. A card token belongs to exactly one customer --
proved on the live store, where account 2 borrowing account 1's token got
"Unable to load payment data" and placed no order. So every card payer costs a
manual capture. Letting most of the pool pay by net terms removes that cost for
those accounts AND makes the mix realistic, rather than trading one for the other.

    _FORCED_PAYMENT = "{{payment_method}}"

now reads the method from each row.

Run:  ./.venv/bin/python tests_per_row_payment_method.py     # expect FAILURES: 0
"""
import re
import sys

sys.path.insert(0, ".")

from apea.agents import generator as G  # noqa: E402

SRC = open(G.__file__, encoding="utf-8").read()
FAILURES = []


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name[:58], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


# --- rebuild the template's resolvers so the test cannot drift from the code --
m = re.search(r"_ADDL_PLACEHOLDER = re\.compile\(\s*\n?\s*(r\"[^\"]+\")\s*\)", SRC)
assert m, "placeholder regex not found"
PH = re.compile(eval(m.group(1)))

HOSTED = ["cybersource", "stripe", "braintree", "adyen", "authorizenet",
          "paypal", "paradoxlabs"]


def row_method(forced, row):
    if "{{" not in (forced or ""):
        return forced or ""
    missing = []

    def _sub(mm):
        col, fb = mm.group(1), mm.group(2)
        v = (row or {}).get(col)
        v = "" if v is None else str(v).strip()
        if not v and fb is not None:
            v = fb.strip()
        if not v:
            missing.append(col)
        return v

    out = PH.sub(_sub, forced).strip()
    return "" if missing else out


def is_card(method):
    mm = (method or "").strip().lower()
    return bool(mm) and any(g in mm for g in HOSTED)


print("a pool can mix card and offline payers")
CARD = {"username": "u1", "payment_method": "paradoxlabs_cybersource",
        "payment_token": "f97d08", "card_cvv": "123"}
NET = {"username": "u2", "payment_method": "netterms"}
check("card row resolves to the gateway",
      row_method("{{payment_method}}", CARD) == "paradoxlabs_cybersource")
check("offline row resolves to net terms",
      row_method("{{payment_method}}", NET) == "netterms")
check("gateway is recognised as a card method", is_card("paradoxlabs_cybersource"))
check("net terms is NOT a card method", not is_card("netterms"))
check("wire payment is NOT a card method", not is_card("wirepayment"))

print()
print("a literal method still forces everyone, exactly as before")
check("literal passes through unchanged",
      row_method("netterms", CARD) == "netterms")
check("empty stays empty (dynamic selection)", row_method("", CARD) == "")

print()
print("the default syntax composes with it")
check("method default fills a blank column",
      row_method("{{payment_method|netterms}}", {"username": "u3"}) == "netterms")
check("column still beats the default",
      row_method("{{payment_method|netterms}}", CARD) == "paradoxlabs_cybersource")

print()
print("fail-closed still applies -- but only to the rows that declared a card")
# A card row with no token must stop the run.
check("card row + missing token -> is a card method, so it must fail closed",
      is_card(row_method("{{payment_method}}", {"payment_method":
                                                "paradoxlabs_cybersource"})))
# An offline row with no token must NOT fail; it never asked for a card.
check("offline row + no token -> not a card method, so no failure",
      not is_card(row_method("{{payment_method}}", NET)))
# A blank method is a data gap, not a card payment silently downgraded.
check("blank method is not treated as a card",
      not is_card(row_method("{{payment_method}}", {"username": "u4"})))

print()
print("the code actually wires this up")
for frag, why in (
        ("def _row_payment_method(row):", "per-row resolver exists"),
        ("def _method_is_card(method):", "card test exists"),
        ("_method = _row_payment_method(row)", "inject_payment uses the row"),
        ("_ADDL_IS_PER_USER and not _method_is_card(_method)",
         "offline rows drop the card additional_data"),
        ("_unresolved and _ADDL_IS_PER_USER and _method_is_card(_method)",
         "fail-closed is gated on the row being a card row")):
    check(why, frag in SRC, "missing: %s" % frag)
check("the old whole-run flag is gone", "_CARD_INTENT" not in SRC)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
