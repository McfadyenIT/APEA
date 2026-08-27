"""Regression guard: the payment additional_data template must be filled in
from the knowledge base, per gateway, and must never be locked.

Run after any change to the payment picker in apea/server.py,
apea/static/index.html, apea/agents/payment.py, or the token_field_by_method
block in knowledge/rules/browser_patterns.yaml:

    ./.venv/bin/python tests_payment_template.py     # expect: FAILURES: 0

Why it exists. The additional_data JSON is boilerplate that differs per gateway
-- CyberSource wants `card_id`, Braintree `paymentMethodNonce`, Stripe
`payment_method`, Adyen `storedPaymentMethodId`. Typing it by hand is a silent
trap: the wrong key is accepted by the UI and only fails at the last step of a
run. Worse, the field's own hint used to read `{"payment_token":"..."}`, which
is not a key ANY gateway accepts.

So APEA fills it in. But it must stay editable: a store may need an extra value
(a 3-D Secure session id, an agreement id), and locking the field would make
that case impossible.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import apea.agents.payment as P          # noqa: E402
import apea.server as S                  # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print("   %-62s %-6s %s" % (label[:62], "OK" if ok else "FAIL",
                                "" if ok else "(got %r want %r)" % (got, want)))
    if not ok:
        fails.append(label)


def template_for(code):
    """The template APEA offers for one payment method code."""
    orig = P.methods_from_recording
    P.methods_from_recording = lambda _f: [{"code": code, "kind": "hosted"}]
    try:
        return S._payment_hints([])["templates"].get(code)
    finally:
        P.methods_from_recording = orig


print("The token key comes from the KB, per gateway")
for code, want in (("paradoxlabs_cybersource", "card_id"),
                   ("paradoxlabs_cybersource_vault", "card_id"),
                   ("braintree_cc_vault", "paymentMethodNonce"),
                   ("stripe_payments", "payment_method"),
                   ("adyen_oneclick", "storedPaymentMethodId"),
                   ("authorizenet_directpost", "card_id"),
                   ("some_unknown_gateway", "public_hash")):
    tpl = json.loads(template_for(code) or "{}")
    key = next((k for k in tpl if k not in ("cc_cid", "save")), None)
    check("%-30s -> %s" % (code, want), key, want)

print("\nThe template points at CSV columns, so each user uses its own card")
tpl = json.loads(template_for("paradoxlabs_cybersource"))
check("the token is a column reference", tpl["card_id"], "{{payment_token}}")
check("so is the CVV", tpl["cc_cid"], "{{card_cvv}}")
check("save is off, so a load run does not store a card per order",
      tpl["save"], False)

print("\nAn offline method gets no card template")
orig = P.methods_from_recording
P.methods_from_recording = lambda _f: [{"code": "netterms", "kind": "offline"}]
try:
    check("netterms has no template", S._payment_hints([])["templates"], {})
finally:
    P.methods_from_recording = orig

print("\nHTTP verbs are not payment methods")
# Every recorded step carries its own "method" key holding the HTTP verb, so a
# careless scan reports POST and GET as payment methods and the picker is junk.
flow = [{"method": "POST", "path": "/checkout/cart/add", "rest": False},
        {"method": "GET", "path": "/rest/V1/carts/mine", "rest": True},
        {"method": "POST", "path": "/rest/V1/carts/mine/payment-information",
         "req": '{"paymentMethod":{"method":"paradoxlabs_cybersource"}}'}]
codes = [m["code"] for m in P.methods_from_recording(flow)]
check("only the real method is reported", codes, ["paradoxlabs_cybersource"])
check("POST is not offered", "POST" in codes, False)
check("GET is not offered", "GET" in codes, False)

print("\nThe field is filled in, never locked")
html = (ROOT / "apea" / "static" / "index.html").read_text(encoding="utf-8")
import re   # noqa: E402
_tag = re.search(r"<input[^>]*id=\"ovPayAddl\"[^>]*>", html)
check("the field exists", bool(_tag), True)
check("and carries no disabled/readonly attribute",
      bool(_tag) and not re.search(r"\b(disabled|readonly)\b", _tag.group(0)), True)
check("nothing disables it in code later",
      "ovPayAddl').disabled" not in html and 'ovPayAddl").disabled' not in html, True)
check("a hand-typed value is not overwritten",
      "cur === addlEl.dataset.filled" in html, True)
check("and a reset back to the default is offered",
      "ovPayAddlReset" in html, True)
check("listeners attach where the picker is built, not only after analysis",
      "pmEl.dataset.wired" in html, True)

print("\nThe misleading hint is gone")
check("no longer suggests payment_token as a key",
      'placeholder=\'e.g. {"payment_token":"..."}\'' in html, False)
check("the method field offers the recorded methods",
      'list="payMethodList"' in html, True)
check("while still allowing a method the recording never used",
      'id="ovPayMethod" type="text"' in html, True)

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
