"""Regression guard for extract_card_tokens.py.

Run:  ./.venv/bin/python tests_card_token_extract.py     # expect: FAILURES: 0

Builds three synthetic captures -- a clean one, one truncated mid-write, and one
that stopped before Place Order -- and checks the extractor pulls the right
token from each account, ignores the token-shaped strings inside the payment
module's JavaScript bundle (source code, not the customer's card), and reports
an incomplete capture instead of failing silently.
"""
import json
import os
import subprocess
import sys

D = '/tmp/hartest'
os.makedirs(D, exist_ok=True)


def entry(url, method="POST", post=None, resp=""):
    e = {"request": {"method": method, "url": url, "headers": []},
         "response": {"status": 200, "content": {"text": resp}}}
    if post is not None:
        e["request"]["postData"] = {"mimeType": "application/json", "text": post}
    return e


def har(entries):
    return {"log": {"version": "1.2", "entries": entries}}


# --- account A: a complete capture ---
payload_a = json.dumps({
    "cartId": "1001",
    "paymentMethod": {
        "method": "paradoxlabs_cybersource",
        "additional_data": {"card_id": "aaaa1111bbbb2222cccc3333dddd4444eeee5555",
                            "cc_cid": "123", "save": False}}})
a = har([
    entry("https://shop.example.com/uk/customer/account/loginPost",
          post="login%5Busername%5D=alice%40yopmail.com&login%5Bpassword%5D=x"),
    # the module's JS bundle mentions card_id -- must NOT be picked up
    entry("https://shop.example.com/static/ParadoxLabs_CyberSource/js/vault.js",
          method="GET", resp='function f(){ var card_id = "NOT_A_REAL_TOKEN_FROM_SOURCE"; }'),
    entry("https://shop.example.com/uk/rest/uk/V1/carts/mine/payment-information",
          post=payload_a, resp='"5001"'),
])
open(os.path.join(D, 'alice.har'), 'w').write(json.dumps(a))

# --- account B: truncated mid-write, but the order call is present ---
payload_b = json.dumps({
    "paymentMethod": {"method": "paradoxlabs_cybersource",
                      "additional_data": {"card_id": "ffff9999eeee8888dddd7777cccc6666bbbb5555",
                                          "cc_cid": "123", "save": True}}})
b = json.dumps(har([
    entry("https://shop.example.com/uk/customer/account/loginPost",
          post="login%5Busername%5D=bob%40yopmail.com&login%5Bpassword%5D=x"),
    entry("https://shop.example.com/uk/rest/uk/V1/carts/mine/payment-information",
          post=payload_b, resp='"5002"'),
]))
open(os.path.join(D, 'bob.har'), 'w').write(b[:-40])   # chop the tail off

# --- account C: stopped before Place Order ---
c = har([
    entry("https://shop.example.com/uk/customer/account/loginPost",
          post="login%5Busername%5D=carol%40yopmail.com&login%5Bpassword%5D=x"),
    entry("https://shop.example.com/uk/rest/uk/V1/carts/mine/shipping-information",
          post='{"addressInformation":{}}'),
])
open(os.path.join(D, 'carol.har'), 'w').write(json.dumps(c))

print("fixtures written to", D)
print()
r = subprocess.run([sys.executable, '/var/www/html/apea/extract_card_tokens.py',
                    os.path.join(D, '*.har')],
                   capture_output=True, text=True)
print("--- stdout (the CSV to paste) ---")
print(r.stdout)
print("--- stderr (detail + warnings) ---")
print(r.stderr)
print("exit code:", r.returncode)

fails = []
if 'alice@yopmail.com,aaaa1111bbbb2222cccc3333dddd4444eeee5555,123' not in r.stdout:
    fails.append("account A token not extracted")
if 'bob@yopmail.com,ffff9999eeee8888dddd7777cccc6666bbbb5555,123' not in r.stdout:
    fails.append("truncated capture (account B) not handled")
if 'NOT_A_REAL_TOKEN_FROM_SOURCE' in r.stdout:
    fails.append("picked a token out of the JS bundle instead of traffic")
if 'carol' not in r.stderr or 'SKIPPED' not in r.stderr:
    fails.append("incomplete capture (account C) not reported")
print()
print("FAILURES:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
