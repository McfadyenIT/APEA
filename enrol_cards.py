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


async def _settle(page, timeout: int = 20000) -> None:
    """Wait for the page to be USABLE, not for the network to go quiet.

    "networkidle" is the obvious choice and it is wrong here. The store runs
    tag manager, session recording, A/B testing, bot detection and device
    fingerprinting, all polling -- the network never goes quiet, so the wait
    always burns its full timeout and then fails. Radwell's login timed out at
    45s this way while the page had in fact loaded in two.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    await page.wait_for_timeout(1200)


# What a card field looks like, in the order the browser's own autofill would
# decide. autocomplete first because it is a standard the gateway is meant to
# set; then name/id; then what the user would read.
_FIELD_HINTS = {
    "number": (("cc-number", "cardnumber"),
               ("card_number", "cardnumber", "card-number", "accountnumber", "pan"),
               ("card number", "kartennummer")),
    "expiry": (("cc-exp",),
               ("card_expiry", "expiry", "exp_date", "expirationdate", "expdate"),
               ("expiry", "expiration", "mm / yy", "mm/yy")),
    "cvc":    (("cc-csc",),
               ("card_cvn", "cvn", "cvv", "cvc", "securitycode", "security_code", "csc"),
               ("security code", "cvv", "cvc", "cvn")),
    "postal": (("postal-code",),
               ("postal_code", "postcode", "zip"),
               ("postal", "zip")),
}


async def _discover_card_fields(frame) -> dict:
    """Read the real card form instead of trusting documented selectors.

    The knowledge base carries CyberSource's PUBLISHED field names and says in
    its own comment that they must be checked against the live site. They did
    not match Radwell: input[name='card_number'] found nothing. The form is
    rendered by the gateway's own JavaScript, so it is not in a capture either
    and cannot be read ahead of time.

    So classify what is actually on the page. This is also the general answer --
    it works for a gateway whose selectors nobody has written down yet.
    """
    try:
        found = await frame.eval_on_selector_all(
            "input, select",
            """els => els.map(e => ({
                 tag: e.tagName.toLowerCase(),
                 name: e.getAttribute('name') || '',
                 id: e.id || '',
                 type: (e.getAttribute('type') || '').toLowerCase(),
                 auto: (e.getAttribute('autocomplete') || '').toLowerCase(),
                 ph: (e.getAttribute('placeholder') || ''),
                 aria: (e.getAttribute('aria-label') || ''),
                 hidden: e.type === 'hidden' || e.offsetParent === null,
               }))""")
    except Exception:
        return {}

    live = [f for f in found if not f["hidden"] and f["type"] != "hidden"]
    out, used = {}, set()
    for field, (autos, names, texts) in _FIELD_HINTS.items():
        for f in live:
            key = (f["name"], f["id"])
            if key in used:
                continue
            hay_name = (f["name"] + " " + f["id"]).lower().replace("-", "_")
            hay_text = (f["ph"] + " " + f["aria"]).lower()
            if (any(a in f["auto"] for a in autos)
                    or any(n in hay_name for n in names)
                    or any(t in hay_text for t in texts)):
                sel = ("[name=\"%s\"]" % f["name"]) if f["name"] else ("#%s" % f["id"])
                out[field] = sel
                used.add(key)
                break
    out["_seen"] = live
    return out


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


async def _dismiss_overlays(page) -> None:
    """Clear the consent banner, which sits on top and swallows the first click.

    Decline is tried before accept: the run only needs the banner out of the
    way, so there is no reason to opt into anything to get it. If neither
    button is there, nothing happens -- this is best-effort by design.
    """
    for sel in ("#onetrust-reject-all-handler",
                "button.ot-pc-refuse-all-handler",
                ".ot-sdk-container button[onclick*='reject' i]",
                "#onetrust-accept-btn-handler",
                "button[aria-label='Close' i]"):
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=3000)
                await page.wait_for_timeout(700)
                return
        except Exception:
            continue


async def _click_when_ready(page, selector: str, timeout: int = 25000) -> bool:
    """Click the first of these selectors that becomes visible.

    Uses wait_for rather than count(). count() answers "is it in the DOM right
    now" and returns 0 for anything the page has not rendered yet -- and these
    pages are 6.9MB, so plenty has not. That single difference is why the last
    run found no add button on a page that has one.
    """
    for sel in [x.strip() for x in (selector or "").split(",") if x.strip()]:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.scroll_into_view_if_needed(timeout=5000)
            await loc.click(timeout=10000)
            return True
        except Exception:
            continue
    return False


async def _add_product(page, row: dict, cfg: dict) -> str:
    """Put the row's product in the basket, using the columns the data file
    already carries.

    The file names the product three ways and different stores key on different
    ones, so try each in turn: search_keyword (what a person would type), then
    sku, then product_id. Searching is preferred over building a product URL
    because URL shape is store-specific -- this store uses
    /buy/<slug>/<id>.html, which cannot be derived from the CSV -- while a
    search box exists on every storefront and uses the column as-is.
    """
    attempts, tried = [], []

    # The product id addresses the product directly through Magento's core
    # route, which this store still serves even though its pretty URLs are
    # /buy/<slug>/<id>.html -- verified against the live site, it returns the
    # product page with an add button. One navigation, no result list to parse.
    pid = str(row.get("product_id") or "").strip()
    if pid:
        attempts.append(("product_id", cfg["product_url"](pid), False))
    # Then search, which needs no knowledge of URL shape at all and uses the
    # column a tester naturally fills in.
    for key in ("search_keyword", "sku"):
        term = str(row.get(key) or "").strip()
        if term and term not in tried:
            tried.append(term)
            attempts.append((key, cfg["search_url"](term), True))

    notes = []
    for key, url, is_search in attempts:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await _dismiss_overlays(page)
            await _settle(page, timeout=30000)
            if is_search:
                # A result list needs one more click to reach the product. If
                # the store redirected a single match straight to the product,
                # there is no list and that is fine.
                await _click_when_ready(page, cfg["sel"]["result_link"], timeout=8000)
                await _dismiss_overlays(page)
                await _settle(page, timeout=30000)
            qty = str(row.get("qty") or 1)
            if qty and qty != "1":
                await _fill_first(page, cfg["sel"]["qty"], qty, timeout=4000)
            if not await _click_when_ready(page, cfg["sel"]["add_to_cart"], timeout=25000):
                notes.append("%s -> no add button" % key)
                continue
            await page.wait_for_timeout(4000)
            return ""
        except Exception as exc:
            notes.append("%s -> %s" % (key, _redact(exc)[:60]))
            continue
    return "add to cart: " + "; ".join(notes or ["no product columns in this row"])


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
        await _dismiss_overlays(page)
        if not await _fill_first(page, cfg["sel"]["login_user"], user):
            return None, "login: could not find the email field"
        if not await _fill_first(page, cfg["sel"]["login_pass"], row.get("password") or ""):
            return None, "login: could not find the password field"
        await page.locator(cfg["sel"]["login_submit"]).first.click()
        try:
            await page.wait_for_url(lambda u: "login" not in str(u).lower(), timeout=30000)
        except Exception:
            pass
        await _settle(page)
        if "login" in (page.url or "").lower():
            return None, ("login: still on the sign-in page after 30s -- wrong "
                          "credentials, or a bot check is holding the form")

        # --- basket ------------------------------------------------------
        step = "add to cart"
        err = await _add_product(page, row, cfg)
        if err:
            return None, err

        # --- checkout ----------------------------------------------------
        step = "checkout"
        await page.goto(cfg["checkout_url"], wait_until="domcontentloaded", timeout=60000)
        await _dismiss_overlays(page)
        await page.wait_for_timeout(cfg["settle_ms"])   # checkout is a slow SPA

        step = "card form"
        frame = await _card_frame(page, cfg["frame_patterns"])
        if frame is not None:
            # Before a single character of the card is typed.
            _assert_frame_allowed(frame.url, cfg["guards"])
        if frame is None:
            return None, ("card form: the gateway iframe never appeared. The account "
                          "may have stalled earlier in checkout (address or delivery "
                          "step) -- run with --headed to watch it.")

        card = cfg["card"]
        # Documented selectors first, then whatever the real form actually has.
        # The published ones did not match this store, so discovery is not a
        # fallback -- it is what makes the script work on a form nobody has
        # written selectors for.
        discovered = await _discover_card_fields(frame)
        sels = dict(cfg["card_sel"])
        for k, v in discovered.items():
            if k != "_seen":
                sels.setdefault(k, v)
                sels[k] = "%s, %s" % (sels[k], v) if sels.get(k) != v else v

        for field, value in (("number", card.get("number")),
                             ("expiry", card.get("exp")),
                             ("cvc", card.get("cvc")),
                             ("postal", card.get("postal"))):
            sel = sels.get(field)
            if not sel or value is None:
                continue
            if not await _fill_first(frame, sel, value) and field in ("number", "cvc"):
                # Report what IS on the form, so the next attempt is informed
                # rather than another guess.
                seen = discovered.get("_seen") or []
                names = ", ".join(
                    (f.get("name") or f.get("id") or "?") for f in seen[:12]) or "nothing"
                return None, ("card form: no %s field found. The form at %s has: %s"
                              % (field, (frame.url or "?").split("?")[0], names))

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
        "search_url": lambda term: base + "/catalogsearch/result/?q=%s" % term,
        "product_url": lambda pid: base + "/catalog/product/view/id/%s" % pid,
        "sel": {
            "login_user": "input[name='login[username]'], input#email, input[name=email]",
            "login_pass": "input[name='login[password]'], input#pass, input[name=password]",
            "login_submit": "button#send2, button[type=submit]",
            "qty": "input#qty, input[name=qty]",
            "add_to_cart": "button#product-addtocart-button, button[title*='Add to Cart' i], "
                           "button[title*='Add to Basket' i], button.tocart",
            "result_link": "a.product-item-link, .product-item-info a.product, "
                           "li.product-item a.product-item-photo",
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
