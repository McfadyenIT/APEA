"""Regression guard: a stored-card token must be resolvable PER VIRTUAL USER.

Run after any change to the payment injection in apea/agents/generator.py:

    ./.venv/bin/python tests_per_user_card_token.py     # expect: FAILURES: 0

A stored-card token belongs to exactly ONE customer -- a token captured on
account A is rejected for account B. So a run driving several accounts needs one
token per account, not one shared across the whole run.

Before this, the payment additional_data was a single dict baked into the
generated script, identical for every VU, which capped card payment at one
account. Now the value may carry {{csv_column}} placeholders that each VU
resolves from its own CSV row:

    {"card_id": "{{payment_token}}", "cc_cid": "{{card_cvv}}", "save": false}

The CSV already carries `payment_token` and `card_cvv` columns, so nothing new
is needed in the data file.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from apea.agents.generator import _assemble_flow_script   # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print("   %-62s %-6s %s" % (label[:62], "OK" if ok else "FAIL",
                                "" if ok else "(got %r want %r)" % (got, want)))
    if not ok:
        fails.append(label)


DISCOVERY = {
    "base_url": "https://shop.example.com",
    "login_form": {"action": "/uk/customer/account/loginPost"},
    "flow": [
        {"name": "Set payment information", "method": "POST",
         "path": "/uk/rest/uk/V1/carts/mine/payment-information",
         "rest": True, "group": "Checkout"},
        {"name": "Add to cart", "method": "POST",
         "path": "/uk/checkout/cart/add/product/1/",
         "rest": False, "group": "PDP"},
    ],
}
PLAN = {"users": 2, "duration_s": 60, "duration_human": "1m", "spawn_rate": 1,
        "label": "Smoke Test", "think_time": [1, 2]}

print("The generated script is valid Python with per-user placeholders in it")
script = _assemble_flow_script(
    DISCOVERY, PLAN, DISCOVERY["flow"], 1, 2,
    payment_method="paradoxlabs_cybersource",
    payment_additional_data='{"card_id":"{{payment_token}}",'
                            '"cc_cid":"{{card_cvv}}","save":false}')
try:
    ast.parse(script)
    check("generated script parses", True, True)
except SyntaxError as e:
    check("generated script parses (line %s: %s)" % (e.lineno, e.msg), False, True)

check("the placeholder survives into the script",
      "{{payment_token}}" in script, True)
check("the resolver is present", "def _row_addl(row):" in script, True)
check("the REST payment path uses it", "_row_addl(self._row)" in script, True)
check("the storefront replay passes the row too",
      "_inject_payment(body, self._row)" in script, True)

print("\nThe resolver behaves correctly, user by user")
fn = re.search(r"(_ADDL_PLACEHOLDER = .*?\n\ndef _row_addl\(row\):.*?\n    return out\n)",
               script, re.S)
if not fn:
    check("resolver extractable for testing", False, True)
else:
    notes = []
    ns = {"re": re, "_clog_annotate": lambda m: notes.append(m)}

    def run(addl, row):
        ns["_PAYMENT_ADDL"] = addl
        exec(fn.group(1), ns)
        return ns["_row_addl"](row)

    PH = {"card_id": "{{payment_token}}", "cc_cid": "{{card_cvv}}", "save": False}

    check("a shared literal token still works unchanged",
          run({"card_id": "abc", "cc_cid": "123", "save": False}, {"username": "a"}),
          {"card_id": "abc", "cc_cid": "123", "save": False})

    check("user 1 gets user 1's token",
          run(PH, {"payment_token": "TOK-A", "card_cvv": "111"}),
          {"card_id": "TOK-A", "cc_cid": "111", "save": False})

    check("user 2 gets user 2's token",
          run(PH, {"payment_token": "TOK-B", "card_cvv": "222"}),
          {"card_id": "TOK-B", "cc_cid": "222", "save": False})

    check("a blank column drops the key rather than sending an empty token",
          run({"card_id": "{{payment_token}}", "cc_cid": "123", "save": False},
              {"payment_token": "   "}),
          {"cc_cid": "123", "save": False})

    check("a missing column drops the key",
          run({"card_id": "{{payment_token}}", "save": False}, {"username": "a"}),
          {"save": False})

    check("no CSV row at all is safe",
          run({"card_id": "{{payment_token}}"}, None), {})

    check("no payment config yields nothing", run({}, {"payment_token": "T"}), {})

    check("dropping a token is reported, not silent", len(notes) >= 3, True)

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
