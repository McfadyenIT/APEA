#!/usr/bin/env python3
"""Pull stored-card tokens out of browser captures, ready to paste into the CSV.

A stored-card token belongs to exactly ONE customer, so a run driving several
accounts needs one token per account. Capturing each one by reading JSON in
DevTools is slow and easy to get wrong. Do this instead:

  1. For each test account: sign in, put something in the basket, reach the
     payment step, then in DevTools -> Network press CLEAR and tick PRESERVE LOG.
  2. Click Place Order. When the confirmation page loads, right-click the
     network list -> "Save all as HAR with content". One file per account.
  3. Run this over all of them:

         python extract_card_tokens.py ~/Downloads/*.har

It prints the `username,payment_token,card_cvv` columns to paste into the test
data CSV, then in APEA set

    Payment additional_data:
        {"card_id": "{{payment_token}}", "cc_cid": "{{card_cvv}}", "save": false}

and every virtual user resolves its own token from its own row.

The capture itself cannot be automated away: entering a card for the first time
is the one step that has to happen in a real browser, because the number goes
straight into the gateway iframe. But it is a ONE-TIME cost per account -- the
token then works for every run until it is revoked.

Card numbers and security codes from the capture are never printed. The CVV
column is emitted as a placeholder for you to fill in; test cards accept any
value, and reading a real one out of a capture is not something this should do.

Reads captures as text rather than JSON, so oversized or truncated HAR exports
still work.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

# The token key is named differently by every gateway.
TOKEN_KEYS = (
    "card_id",             # ParadoxLabs CyberSource / Magento vault
    "public_hash",         # Magento vault
    "payment_method",      # Stripe (pm_...)
    "paymentMethodNonce",  # Braintree
    "storedPaymentMethodId",   # Adyen
    "payment_token",
)
TOKEN_RE = re.compile(
    r'\\?"(' + "|".join(TOKEN_KEYS) + r')\\?"\s*:\s*\\?"([A-Za-z0-9_\-]{8,})',
    re.I)
# Login bodies are form-encoded, so the address arrives as name%40host.
EMAIL_RE = re.compile(r'[A-Za-z0-9._+\-]+(?:@|%40)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}',
                      re.I)
METHOD_RE = re.compile(r'\\?"method\\?"\s*:\s*\\?"([a-z][a-z0-9_]{3,40})\\?"')
# Order-placement calls, by platform.
ORDER_RE = re.compile(
    r'carts/mine/(?:set-)?payment-information|checkout/onepage/saveOrder'
    r'|/place-?order|checkout/submit|/payments(?:/details)?\b', re.I)

# Never emit these, whatever the capture holds.
PAN_RE = re.compile(r'\b\d{12,19}\b')


def read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def account_of(txt: str) -> str:
    """Best guess at whose capture this is: the most frequent non-vendor email."""
    counts: dict[str, int] = {}
    for e in EMAIL_RE.findall(txt):
        low = e.lower().replace("%40", "@").rstrip(".")
        if any(d in low for d in ("sentry", "newrelic", "example.com", "@2x",
                                  "cybersource", "google", "cloudflare")):
            continue
        counts[low] = counts.get(low, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def tokens_of(txt: str) -> list[tuple[str, str]]:
    """(key, value) for every token that appears in an ORDER call, newest last.

    Scoped to order calls on purpose: the same words appear in the payment
    module's JavaScript bundle, which is source code, not the customer's card.
    """
    found: list[tuple[str, str]] = []
    seen = set()
    for m in ORDER_RE.finditer(txt):
        window = txt[m.start():m.start() + 8000]
        for t in TOKEN_RE.finditer(window):
            key, val = t.group(1), t.group(2)
            if (key, val) in seen:
                continue
            seen.add((key, val))
            found.append((key, val))
    return found


def method_of(txt: str) -> str:
    for m in ORDER_RE.finditer(txt):
        window = txt[m.start():m.start() + 8000]
        mm = METHOD_RE.search(window)
        if mm:
            return mm.group(1)
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract stored-card tokens from HAR captures for the APEA test CSV.")
    ap.add_argument("captures", nargs="+",
                    help="HAR files, one per test account (globs allowed)")
    ap.add_argument("--cvv", default="123",
                    help="CVV to write in the card_cvv column (default: 123)")
    ap.add_argument("-o", "--out", help="write the CSV here instead of stdout")
    args = ap.parse_args(argv)

    paths: list[str] = []
    for pat in args.captures:
        paths.extend(sorted(glob.glob(pat)) or [pat])

    rows, problems = [], []
    for path in paths:
        name = os.path.basename(path)
        if not os.path.exists(path):
            problems.append("%s: not found" % name)
            continue
        txt = read(path)
        toks = tokens_of(txt)
        acct = account_of(txt)
        meth = method_of(txt)
        if not toks:
            why = ("no order call in the capture -- it probably stopped before "
                   "Place Order" if not ORDER_RE.search(txt)
                   else "order call found but it carries no stored-card token; "
                        "was the card saved to the account?")
            problems.append("%s: %s" % (name, why))
            continue
        key, val = toks[-1]
        rows.append((acct or "<unknown>", val, key, meth, name))

    out = []
    out.append("username,payment_token,card_cvv")
    for acct, val, _k, _m, _f in rows:
        out.append("%s,%s,%s" % (acct, val, args.cvv))
    body = "\n".join(out) + "\n"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print("wrote %s (%d account(s))" % (args.out, len(rows)))
    else:
        print(PAN_RE.sub("***", body), end="")

    if rows:
        print("\n# detail", file=sys.stderr)
        for acct, val, k, m, f in rows:
            print("#   %-28s %-14s %s...  (%s)"
                  % (acct, k, val[:12], m or "method?"), file=sys.stderr)
        keys = {r[2] for r in rows}
        print("#\n# In APEA set Payment additional_data to:", file=sys.stderr)
        print('#   {"%s": "{{payment_token}}", "cc_cid": "{{card_cvv}}", '
              '"save": false}' % sorted(keys)[0], file=sys.stderr)
        if len(keys) > 1:
            print("# WARNING: captures used different token keys (%s). Split them "
                  "into separate runs -- one key per run." % ", ".join(sorted(keys)),
                  file=sys.stderr)

    for p in problems:
        print("# SKIPPED %s" % p, file=sys.stderr)
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
