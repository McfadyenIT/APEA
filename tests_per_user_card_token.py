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
fn = re.search(r"(_ADDL_PLACEHOLDER = .*?\ndef _row_addl\(row\):.*?\n    return out\n)",
               script, re.S)
if not fn:
    check("resolver extractable for testing", False, True)
else:
    notes = []
    ns = {"re": re, "_clog_annotate": lambda m: notes.append(m),
          # Card intent used to be one flag for the whole run. It is now decided
          # per ROW, because a pool can mix card payers with net-terms payers,
          # so the namespace needs the gateway list that the row test consults.
          "_ADDL_IS_PER_USER": False, "_FORCED_PAYMENT": "", "_FLOW": {},
          "_HOSTED_GATEWAYS": ["cybersource", "paradoxlabs", "stripe"],
          "_flush": lambda: None}

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

    print("\nA DECLARED card payment fails the run instead of paying by invoice")

    # With intent declared, an unresolvable token must raise the fail-closed
    # flags rather than quietly leaving the key out.
    ns["_ADDL_IS_PER_USER"] = True
    ns["_FORCED_PAYMENT"] = "paradoxlabs_cybersource"
    flow = {}
    ns["_FLOW"] = flow
    ns["_flush"] = lambda: None
    run({"card_id": "{{payment_token}}", "save": False}, {"payment_token": ""})
    check("payment_required is raised", flow.get("payment_required"), True)
    check("payment_ok is false", flow.get("payment_ok"), False)
    check("the reason names the missing column",
          "payment_token" in str(flow.get("payment_err", "")), True)
    check("the reason states there is no fallback",
          "NOT fall back" in str(flow.get("payment_err", "")), True)

    # Without declared intent (no forced gateway) nothing is failed.
    ns["_ADDL_IS_PER_USER"] = False
    flow2 = {}
    ns["_FLOW"] = flow2
    run({"card_id": "{{payment_token}}", "save": False}, {"payment_token": ""})
    check("an undeclared run is left alone", flow2.get("payment_required"), None)

print("\nThe two tracks share one fail-closed gate")
check("the generator declares a card-intent gate", "_ADDL_IS_PER_USER" in script, True)
check("intent is DECLARED, never inferred from a blank cell",
      "_unresolved and _ADDL_IS_PER_USER and _method_is_card(_method)" in script, True)
check("intent is decided per ROW, not once for the whole run",
      "_method = _row_payment_method(row)" in script, True)

exe = (ROOT / "apea" / "agents" / "executor.py").read_text(encoding="utf-8")
check("the executor fails the run on an unmet HTTP-track intent",
      "_http_req and not _http_ok" in exe, True)
check("the browser track's gate is still honoured",
      "browser_payment_required" in exe, True)

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
