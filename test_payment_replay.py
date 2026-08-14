"""Offline test for the payment replay-profile classifier (no browser needed).

Feeds synthetic captured payment sequences into payment_replay.classify and prints
the verdict, so you can validate the classifier + KB (browser_patterns.yaml ->
replay_profiles) without running a full browser-track load.

Run:  .venv\\Scripts\\python.exe test_payment_replay.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apea.agents import payment_replay  # noqa: E402


# --- synthetic browser_track.json summaries (what Track B would have captured) ---

STRIPE = {
    "gateway": "stripe",
    "payment_network": [
        {"method": "POST", "url": "https://api.stripe.com/v1/payment_methods",
         "status": 200, "post_data": "type=card&card[number]=[REDACTED_PAN]"},
        {"method": "POST", "url": "https://merchant.example.com/checkout/confirm",
         "status": 200, "post_data": "payment_method=pm_[REDACTED]"},
    ],
}

CYBERSOURCE = {
    "gateway": "paradoxlabs_cybersource",
    "payment_network": [
        {"method": "POST", "url": "https://store.example.com/paradoxlabs/cybersource/getParams",
         "status": 200, "post_data": "signature=[REDACTED]&transaction_uuid=abc123&access_key=[REDACTED]"},
        {"method": "POST", "url": "https://testsecureacceptance.cybersource.com/embedded/checkout_update",
         "status": 200, "post_data": "card_number=[REDACTED_PAN]&signature=[REDACTED]"},
    ],
}

# unknown gateway, but a tokenization endpoint is visible in the capture
UNKNOWN_TOKENIZE = {
    "gateway": "",
    "payment_network": [
        {"method": "POST", "url": "https://pay.somegw.com/v1/tokens",
         "status": 200, "post_data": "card=[REDACTED_PAN]"},
    ],
}

EMPTY = {"gateway": "", "payment_network": []}


def show(name, bt):
    print("=" * 70)
    print(name)
    r = payment_replay.classify(bt)
    if r is None:
        print("  -> classify returned None (nothing to assess)")
        return r
    print("  gateway    :", r["gateway"])
    print("  replayable :", r["replayable"], "(%s confidence)" % r["confidence"])
    print("  verdict    :", r["verdict"])
    if r.get("mint"):
        print("  mint       :", r["mint"].get("resolved_mint_endpoint")
              or r["mint"].get("mint_endpoint_hint"))
    if r.get("reasons"):
        print("  reasons    :")
        for x in r["reasons"]:
            print("     -", x)
    if r.get("blockers"):
        print("  blockers   :")
        for x in r["blockers"]:
            print("     -", x)
    return r


if __name__ == "__main__":
    a = show("STRIPE (expect: replayable=True)", STRIPE)
    b = show("CYBERSOURCE (expect: conditional + single-use blocker)", CYBERSOURCE)
    c = show("UNKNOWN gateway w/ tokenize endpoint (expect: conditional)", UNKNOWN_TOKENIZE)
    d = show("EMPTY (expect: None)", EMPTY)

    print("=" * 70)
    ok = True
    ok &= (a and a["replayable"] in (True, "true"))
    ok &= (b and b["replayable"] == "conditional" and bool(b["blockers"]))
    ok &= (c and c["replayable"] == "conditional")
    ok &= (d is None)
    print("SMOKE TEST:", "PASS ✅" if ok else "FAIL ❌")
    sys.exit(0 if ok else 1)
