#!/usr/bin/env python3
"""Capture a card token for every account that needs one, without a human doing
ten checkouts by hand.

WHY THIS EXISTS
---------------
A stored-card token belongs to exactly ONE customer. Proved on a live store:
account 2 presenting account 1's token got "Unable to load payment data" and
placed no order, even though both were the same physical card. So a pool of ten
card-paying accounts needs ten tokens, and until now each one cost a hand-driven
checkout plus a hunt through DevTools.

Nothing else can produce a token on that store. All of these were tried and
came back empty: the TokenBase REST API (disabled by store config), the GraphQL
vault, /vault/cards/listaction, the ParadoxLabs account page, and every
customer-data section. The checkout offers no "save this card" control and sends
save:false every time -- yet the token it mints is fully reusable. The token is
therefore a BY-PRODUCT OF CHECKING OUT, and the only way to get one is to check
out. This script does that, once per account, unattended.

WHAT IT DOES NOT DO
-------------------
It does not take a card number from your data file. Card details never belong in
a test-data spreadsheet -- those files get copied, mailed and committed. The card
comes from the knowledge base's PUBLIC CyberSource sandbox test card, and the
safeguards recorded alongside it are enforced here before a browser opens:
sandbox host only, no real PAN, never logged, never written to disk.

USAGE
-----
    python enrol_cards.py --csv apea_enrol_pool_10.csv \\
                          --base-url https://mcstaging.radwell.eu/uk \\
                          --headed

Accounts that already carry a token are skipped, so re-running costs nothing and
a partial failure can simply be re-run.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# The token as it travels: the store's own field name, inside additional_data.
_TOKEN_RE = re.compile(r'"card_id"\s*:\s*"([0-9A-Za-z_-]{8,})"')
# Anything that looks like a card number, so it can be kept OUT of every message.
_PAN_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _redact(text: str) -> str:
    """No card number reaches a log line, an exception, or the console."""
    return _PAN_RE.sub("[card redacted]", str(text or ""))


def _host_matches(host: str, pattern: str) -> bool:
    """Match a host against a rule on LABEL boundaries.

    A naive substring test gets this exactly backwards. The sandbox host
    "testsecureacceptance.cybersource.com" CONTAINS the production host
    "secureacceptance.cybersource.com", so `in` would refuse the one host the
    script is supposed to use and only that one. Equality, or a dot-prefixed
    suffix, is the correct test.
    """
    h = (host or "").strip().strip(".").lower()
    p = (pattern or "").strip().strip(".").lower()
    return bool(h) and bool(p) and (h == p or h.endswith("." + p))


def _load_kb() -> dict:
    """The CyberSource browser profile: card selectors, sandbox test cards and
    the safeguards. Reused rather than restated so there is one source."""
    try:
        from apea.agents.browser_runner_gen import _load_browser_patterns
        return _load_browser_patterns() or {}
    except Exception as exc:                      # pragma: no cover - import guard
        raise SystemExit("cannot read the knowledge base: %s" % exc)


def _gateway_profile(kb: dict, gateway: str) -> dict:
    """Match the CSV's method code to a KB profile by substring, the same test
    the rest of APEA uses, so 'paradoxlabs_cybersource' finds 'cybersource'."""
    g = (gateway or "").lower()
    for code, prof in (kb or {}).items():
        if not isinstance(prof, dict):
            continue
        if code.lower() in g or g in code.lower():
            return prof
    for code, prof in (kb or {}).items():
        if isinstance(prof, dict) and any(
                str(p).split(".")[0] in g for p in (prof.get("frame_url_patterns") or [])):
            return prof
    return {}


def _check_safeguards(prof: dict, base_url: str, allow_unknown_card: bool,
                      card_name: str = "success") -> dict:
    """Refuse to run outside the conditions the knowledge base already recorded
    for driving a card form. These were written down BEFORE anything was built
    and they are the reason this script is safe to run unattended.

    card_name picks one of the gateway's published test cards. A merchant's
    sandbox may not accept every brand's canonical number, so the choice has to
    be available -- but only from the KB list, never a number typed on the
    command line.
    """
    guards = (prof.get("safeguards") or {})
    cards = (prof.get("test_cards") or {})
    card = cards.get(card_name)
    if card is None and card_name != "success":
        raise SystemExit("no test card named %r for this gateway -- available: %s"
                         % (card_name, ", ".join(sorted(cards)) or "none"))
    card = card or cards.get("success") or (list(cards.values())[0] if cards else None)
    if not card:
        raise SystemExit("no sandbox test card in the knowledge base for this gateway "
                         "-- refusing to prompt for a real one")

    if guards.get("real_pan_prohibited") and not allow_unknown_card:
        known = {re.sub(r"\D", "", str(c.get("number", ""))) for c in cards.values()}
        if re.sub(r"\D", "", str(card.get("number", ""))) not in known:
            raise SystemExit("the configured card is not one of the knowledge base's "
                             "public sandbox cards -- refusing")

    # Defensive only. base_url is the STORE, and the forbid/allow lists name
    # GATEWAY hosts, so this can only fire on an obviously wrong invocation. The
    # check that matters is _assert_frame_allowed, applied to the iframe the card
    # is actually typed into -- see _card_frame.
    host = (urlparse(base_url).hostname or "")
    for bad in (guards.get("forbid_submit_hosts") or []):
        if _host_matches(host, str(bad)):
            raise SystemExit("%s is a PRODUCTION gateway host -- refusing to drive a "
                             "card form against it" % bad)
    return card


def _assert_frame_allowed(frame_url: str, guards: dict) -> None:
    """The real safeguard: refuse to type a card into a form that is not the
    sandbox. Applied to the iframe URL at the moment it is found, which is the
    only place the destination is actually known.
    """
    host = (urlparse(frame_url).hostname or "")
    for bad in (guards.get("forbid_submit_hosts") or []):
        if _host_matches(host, str(bad)):
            raise SystemExit("the card form is hosted on %s, a PRODUCTION gateway "
                             "-- refusing to type a card into it" % host)
    allowed = [str(h) for h in (guards.get("allowed_submit_hosts") or []) if str(h).strip()]
    if allowed and not any(_host_matches(host, a) for a in allowed):
        raise SystemExit("the card form is hosted on %s, which is not in the knowledge "
                         "base's allowed sandbox hosts (%s) -- refusing"
                         % (host or "?", ", ".join(allowed)))


def _card_selectors(prof: dict) -> dict:
    """Secure Acceptance's hosted form first, Flex Microform second -- the KB
    carries both because a merchant may run either."""
    return (prof.get("card_element_fields")
            or prof.get("payment_element_fields") or {})


def _needs_token(row: dict, hosted: list) -> bool:
    """An account needs enrolling when it DECLARES a card method and has no
    token. A row paying by an offline method needs nothing and is left alone."""
    method = (row.get("payment_method") or "").strip().lower()
    if not method or not any(h in method for h in hosted):
        return False
    return not (row.get("payment_token") or "").strip()


async def _fill_first(scope, selector: str, value: str, timeout: int = 8000) -> bool:
    """Fill the first selector in a comma-separated list that actually exists.
    The KB gives alternates because merchants differ; trying each in turn is
    what makes one profile serve more than one store."""
    for sel in [s.strip() for s in (selector or "").split(",") if s.strip()]:
        try:
            loc = scope.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.fill(str(value))
            return True
        except Exception:
            continue
    return False


async def _card_frame(page, patterns: list, timeout: float = 20.0):
    """The card fields live in the gateway's own cross-origin iframe. Wait for a
    frame whose URL matches the KB's patterns rather than guessing an index."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for fr in page.frames:
            url = (fr.url or "").lower()
            if any(str(p).lower() in url for p in patterns):
                return fr
        await asyncio.sleep(0.4)
    return None


async def enrol_one(browser, row: dict, cfg: dict) -> tuple[str | None, str]:
    """Drive one account through checkout and return (token, note).

    Fail-closed and LOUD: every failure names the step it reached, because the
    first run against a new store always needs tuning and a vague error costs
    more time than the manual capture would have.
    """
    user = (row.get("username") or "").strip()
    captured: dict[str, str] = {}

    ctx = await browser.new_context(ignore_https_errors=True)
    page = await ctx.new_page()

    def _on_request(req):
        # The token is only ever in the request the browser SENDS -- it appears
        # in no response anywhere. This listener is the whole point of the run.
        try:
            body = req.post_data
        except Exception:
            return
        if not body or "card_id" not in body:
            return
        m = _TOKEN_RE.search(body)
        if m:
            captured["token"] = m.group(1)

    page.on("request", _on_request)

    step = "start"
    try:
        # --- sign in -----------------------------------------------------
        step = "login"
        await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=45000)
        if not await _fill_first(page, cfg["sel"]["login_user"], user):
            return None, "login: could not find the email field"
        if not await _fill_first(page, cfg["sel"]["login_pass"], row.get("password") or ""):
            return None, "login: could not find the password field"
        await page.locator(cfg["sel"]["login_submit"]).first.click()
        await page.wait_for_load_state("networkidle", timeout=45000)
        if "login" in (page.url or "").lower():
            return None, "login: still on the sign-in page -- wrong credentials?"

        # --- basket ------------------------------------------------------
        step = "add to cart"
        for url in cfg["product_urls"](row):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if await _fill_first(page, cfg["sel"]["qty"], str(row.get("qty") or 1),
                                     timeout=3000):
                    pass
                await page.locator(cfg["sel"]["add_to_cart"]).first.click(timeout=15000)
                await page.wait_for_timeout(2500)
                break
            except Exception:
                continue

        # --- checkout ----------------------------------------------------
        step = "checkout"
        await page.goto(cfg["checkout_url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(cfg["settle_ms"])

        step = "card form"
        frame = await _card_frame(page, cfg["frame_patterns"])
        if frame is not None:
            # Before a single character of the card is typed.
            _assert_frame_allowed(frame.url, cfg["guards"])
        if frame is None:
            return None, ("card form: the gateway iframe never appeared. The account "
                          "may have stalled earlier in checkout (address or delivery "
                          "step) -- run with --headed to watch it.")

        sels, card = cfg["card_sel"], cfg["card"]
        for field, value in (("number", card.get("number")),
                             ("expiry", card.get("exp")),
                             ("cvc", card.get("cvc")),
                             ("postal", card.get("postal"))):
            sel = sels.get(field)
            if not sel or value is None:
                continue
            if not await _fill_first(frame, sel, value) and field in ("number", "cvc"):
                return None, "card form: no %s field matched %s" % (field, sel)

        step = "place order"
        await page.locator(cfg["sel"]["place_order"]).first.click(timeout=20000)
        for _ in range(60):
            if captured.get("token"):
                break
            await page.wait_for_timeout(1000)

        token = captured.get("token")
        if token:
            return token, "enrolled"
        return None, ("place order: no card_id crossed the wire within 60s. If a "
                      "3-D Secure challenge appeared, this account needs a person.")
    except Exception as exc:
        return None, "%s: %s" % (step, _redact(exc)[:180])
    finally:
        await ctx.close()


async def run(args) -> int:
    kb = _load_kb()
    prof = _gateway_profile(kb, args.gateway)
    if not prof:
        raise SystemExit("no knowledge-base profile matches gateway %r" % args.gateway)
    card = _check_safeguards(prof, args.base_url, args.allow_unknown_card, args.card)
    guards = prof.get("safeguards") or {}

    src = Path(args.csv)
    rows = list(csv.DictReader(io.open(src, encoding="utf-8-sig")))
    if not rows:
        raise SystemExit("%s has no rows" % src)

    hosted = [str(h).lower() for h in (args.hosted or "cybersource,paradoxlabs,stripe,"
                                       "braintree,adyen,authorizenet").split(",")]
    todo, seen = [], set()
    for r in rows:
        u = (r.get("username") or "").strip().lower()
        if u and u not in seen and _needs_token(r, hosted):
            seen.add(u)
            todo.append(r)

    print("using the %r sandbox card (ending %s) from the knowledge base"
          % (args.card, str(card.get("number", ""))[-4:]))
    print("%d row(s), %d account(s) needing a token" % (len(rows), len(todo)))
    if not todo:
        print("nothing to do -- every card account already carries one")
        return 0

    base = args.base_url.rstrip("/")
    cfg = {
        "login_url": base + "/customer/account/login/",
        "checkout_url": base + "/checkout/",
        "settle_ms": args.settle_ms,
        "frame_patterns": prof.get("frame_url_patterns") or [],
        "card_sel": _card_selectors(prof),
        "card": card,
        "guards": guards,
        "product_urls": lambda r: [
            u for u in (base + "/catalog/product/view/id/%s" % (r.get("product_id") or ""),
                        base + "/catalogsearch/result/?q=%s" % (r.get("sku") or ""))
            if (r.get("product_id") or r.get("sku"))],
        "sel": {
            "login_user": "input[name='login[username]'], input#email, input[name=email]",
            "login_pass": "input[name='login[password]'], input#pass, input[name=password]",
            "login_submit": "button#send2, button[type=submit]",
            "qty": "input#qty, input[name=qty]",
            "add_to_cart": "button#product-addtocart-button, button[title*='Add to Cart' i]",
            "place_order": "button[title*='Place Order' i], button.checkout, "
                           "button[data-role='review-save']",
        },
    }

    from playwright.async_api import async_playwright

    results: dict[str, str] = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        try:
            for i, row in enumerate(todo, 1):
                user = (row.get("username") or "").strip()
                print("  [%d/%d] %-32s " % (i, len(todo), user), end="", flush=True)
                token, note = await enrol_one(browser, row, cfg)
                if token:
                    results[user.lower()] = token
                    print("OK  %s..." % token[:12])
                else:
                    print("FAILED  %s" % note)
        finally:
            await browser.close()

    if not results:
        print("\nno tokens captured -- nothing written")
        return 1

    shutil.copyfile(src, src.with_suffix(src.suffix + ".bak"))
    for r in rows:
        u = (r.get("username") or "").strip().lower()
        if u in results and not (r.get("payment_token") or "").strip():
            r["payment_token"] = results[u]
    with io.open(src, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("\n%d token(s) written to %s (original kept as %s.bak)"
          % (len(results), src.name, src.name))
    print("no card number was written anywhere, and none is needed to run the test")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", required=True, help="test-data CSV to fill in, edited in place")
    p.add_argument("--base-url", required=True, help="store base URL including any store path")
    p.add_argument("--gateway", default="cybersource", help="gateway profile to use")
    p.add_argument("--hosted", default="", help="comma-separated card method substrings")
    p.add_argument("--settle-ms", type=int, default=6000,
                   help="pause after checkout loads before looking for the card form")
    p.add_argument("--card", default="success",
                   help="which of the gateway's published test cards to use "
                        "(success, decline, threeds, mastercard, amex - depends on gateway)")
    p.add_argument("--headed", action="store_true", help="show the browser (use when tuning)")
    p.add_argument("--allow-unknown-card", action="store_true",
                   help=argparse.SUPPRESS)
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
