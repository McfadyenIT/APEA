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
_PAN_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


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


# Gateways where the shopper LEAVES the store to authorise. There is no card
# form to fill: the flow redirects to the provider, the shopper signs in to an
# account there, and the store gets an agreement back. Nothing this script does
# applies, and pretending otherwise would burn an order to find that out.
_REDIRECT_GATEWAYS = ("paypal", "klarna", "amazon", "sofort", "ideal",
                      "bancontact", "giropay", "afterpay", "clearpay", "affirm")


def _classify_row(row: dict, hosted: list, kb: dict) -> tuple:
    """What should happen to this row? Returns (action, reason).

    Three outcomes, and the difference matters. An offline payer needs nothing
    and its silence is correct. A card payer needs enrolling. A REDIRECT gateway
    needs a person and must say so -- it was previously skipped with no message
    at all, which reads exactly like "handled".
    """
    method = (row.get("payment_method") or "").strip()
    low = method.lower()
    if not method:
        return ("skip", "no payment method declared")
    if (row.get("payment_token") or "").strip():
        return ("skip", "already has a token")
    if any(g in low for g in _REDIRECT_GATEWAYS):
        return ("cannot", "%s sends the shopper to the provider to sign in; there is "
                          "no card form to fill. Enrol it by hand, or have those "
                          "accounts pay by an offline method." % method)
    if not any(h in low for h in hosted):
        return ("skip", "%s needs no card" % method)
    if not _gateway_profile(kb, method):
        return ("cannot", "no knowledge-base profile for %s, so its card form and "
                          "test cards are unknown" % method)
    return ("enrol", "")


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


async def _fill_all(scope, selector: str, value: str, timeout: int = 2500) -> int:
    """Fill every box matching, not just the first.

    This checkout asks for the same thing twice -- "Contact Details" and
    "Shipping Address" each have First Name and Last Name -- so filling only the
    first leaves the second one empty and red, and checkout will not move on.
    Returns how many were filled.
    """
    n = 0
    for sel in [x.strip() for x in (selector or "").split(",") if x.strip()]:
        try:
            loc = scope.locator(sel)
            count = await loc.count()
        except Exception:
            continue
        for i in range(count):
            try:
                one = loc.nth(i)
                if not await one.is_visible():
                    continue
                await one.fill(str(value), timeout=timeout)
                n += 1
            except Exception:
                continue
    return n


async def _card_frame(page, patterns: list, timeout: float = 45.0):
    """Find the gateway frame that actually CONTAINS the card fields.

    Matching on URL alone returns the wrong frame for two reasons, and the last
    run hit both: it reported "the form at .../embedded/checkout_load has:
    nothing".

    The frame EXISTS before its fields do. checkout_load is the container the
    gateway then renders into, so reading it the instant its URL matches gives
    an empty document. And Secure Acceptance nests -- the frame carrying the
    matching URL may be the parent of the one holding the inputs.

    So keep looking, through children too, until a frame has real inputs. If the
    time runs out, return the best match anyway so the caller can report what it
    did see rather than just "nothing appeared".
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    best = None
    while loop.time() < deadline:
        cands = [f for f in page.frames
                 if any(str(p).lower() in (f.url or "").lower() for p in patterns)]
        for f in list(cands):
            try:
                cands.extend(f.child_frames)
            except Exception:
                pass
        for f in cands:
            best = best or f
            try:
                n = await f.eval_on_selector_all(
                    "input:not([type=hidden]), select", "els => els.length")
            except Exception:
                continue
            if n:
                return f
        await asyncio.sleep(0.6)
    return best


# CSV column -> the form control that holds it. Magento's own field names,
# which this store's checkout uses. Each entry lists alternates because themes
# rename things; the first one present wins.
_ADDRESS_MAP = (
    ("username",   "input[name='username'], input#customer-email, "
                   "input[name='email']"),
    # This checkout collects the same person TWICE, under two naming schemes:
    # "Contact Details" uses guest* names, "Shipping Address" uses the plain
    # ones. Filling only the plain set left Contact Details empty and red, and
    # the page then refused to advance with no error text and no clue -- the
    # capture is what finally showed both sets side by side.
    ("firstname",  "input[name='firstname'], input[name='guestfirstname']"),
    ("lastname",   "input[name='lastname'], input[name='guestlastname']"),
    # Required on this store, and marked with a red asterisk. There is no
    # column for it in older data files, so --company supplies a default.
    ("company",    "input[name='company'], input[name='guestcompanyname']"),
    ("vat_id",     "input[name='vat_id'], input[name='guestvat']"),
    ("street",     "input[name='street[0]'], input[name='street'], "
                   "input#street_1"),
    ("city",       "input[name='city']"),
    ("postcode",   "input[name='postcode']"),
    ("telephone",  "input[name='telephone']"),
)


async def _fill_shipping(page, row: dict, cfg: dict) -> str:
    """Type the delivery address from the data file.

    The checkout will not move on without it, and it was never being filled --
    the run stopped on an address form waiting for a person. Every field it asks
    for is already a column: first name, last name, street, city, postcode,
    phone, country, county. So fill them from the row rather than asking anyone.

    Every field is best-effort. A saved address means most of these are not on
    the page at all, and that is a success, not a failure -- the proof is
    whether checkout advances, which the caller checks.
    """
    # The checkout is a single-page app: the address form is drawn after the
    # page loads, so typing straight away types into nothing. Wait for a field
    # to actually be on screen first. This is why the operator had to fill the
    # form by hand -- the script had already been and gone.
    try:
        await page.locator(
            "input[name='firstname'], input[name='lastname'], input[name='city']"
        ).first.wait_for(state="visible", timeout=40000)
    except Exception:
        # No address field in 40s. Distinguish "still loading" from "not there",
        # because they need opposite fixes and look identical in a log.
        try:
            spinning = await page.locator(
                ".loading-mask, .loader, [data-role='loader']").first.is_visible()
        except Exception:
            spinning = False
        if spinning:
            return ("CHECKOUT NEVER FINISHED LOADING -- still showing a spinner after "
                    "40s. A basket left full by earlier runs is the usual cause; "
                    "empty it at %s and try again." % cfg.get("cart_url", "the cart"))
    await page.wait_for_timeout(1500)

    filled = []
    for col, sel in _ADDRESS_MAP:
        val = str(row.get(col) or "").strip()
        if not val and col == "company":
            val = str(cfg.get("company") or "").strip()
        if not val:
            continue
        n = await _fill_all(page, sel, val)
        if n:
            filled.append("%s x%d" % (col, n) if n > 1 else col)

    # Country and county are dropdowns whose options are loaded, so they need
    # select_option rather than fill -- and the county control changes shape
    # per country: a list where the platform knows the regions, a free text box
    # where it does not.
    country = str(row.get("country_id") or "").strip()
    if country:
        for sel in ("select[name='country_id']", "select#country"):
            try:
                el = page.locator(sel).first
                if await el.count():
                    await el.select_option(value=country, timeout=5000)
                    filled.append("country_id")
                    await page.wait_for_timeout(1200)
                    break
            except Exception:
                continue

    region = (str(row.get("region") or "").strip()
              or str(row.get("region_code") or "").strip())
    if region:
        done = False
        for sel in ("select[name='region_id']", "select#region_id"):
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.select_option(label=region, timeout=5000)
                    done = True
                    break
            except Exception:
                continue
        if not done:
            if await _fill_first(page, "input[name='region']", region, timeout=2500):
                done = True
        if done:
            filled.append("region")

    if filled:
        # The address triggers a delivery-price lookup; it must land before the
        # methods appear.
        await page.wait_for_timeout(3500)
        return ", ".join(filled)

    # Nothing matched. Rather than fail with "no fields filled", report the
    # fields the form ACTUALLY has -- the same move that solved the card form
    # and the Place Order button. One run then tells us the real names.
    try:
        real = await page.eval_on_selector_all(
            "input, select",
            """els => els.filter(e => e.offsetParent && e.type !== 'hidden')
                        .map(e => (e.getAttribute('name') || e.id || '?')
                                  + (e.value ? '=filled' : ''))
                        .slice(0, 25)""")
    except Exception:
        real = []
    return "NOTHING FILLED -- the form has: %s" % (", ".join(real) or "no visible fields")


async def _reach_payment_step(page, cfg, method: str) -> str:
    """Walk checkout far enough that the card form is asked to render.

    The gateway does not draw its form until its payment method is SELECTED --
    Magento's checkout shows a list and renders the chosen one. The script was
    waiting for a form nothing had asked for. It also has to get past the
    delivery step first, which is where the other account stalled.

    Best-effort throughout: a store that shows payment immediately just finds
    nothing to click, and that is a success, not a failure.
    """
    filled = await _fill_shipping(page, cfg["row"], cfg)

    # Delivery method, then Next. Some themes preselect; clicking a radio that
    # is already chosen is harmless.
    try:
        radio = page.locator(cfg["sel"]["ship_method"]).first
        if await radio.count() and await radio.is_visible():
            await radio.click(timeout=6000)
            await page.wait_for_timeout(1500)
    except Exception:
        pass
    advanced = await _click_when_ready(page, cfg["sel"]["continue"], timeout=20000)
    if not advanced:
        # Name what was on screen. Guessing a button's wording is what cost the
        # last two runs; the page can just tell us.
        try:
            _labels = await page.eval_on_selector_all(
                "button, input[type=submit], a.action",
                "els => els.filter(e => e.offsetParent)"
                ".map(e => (e.innerText || e.value || '').trim())"
                ".filter(Boolean).slice(0, 16)")
        except Exception:
            _labels = []
        _step_note = ("could not advance past the delivery step. Visible buttons: %s%s"
                      % (", ".join(_labels) or "none",
                         await _capture(page,
                                        (cfg.get("row") or {}).get("username") or "user",
                                        "delivery-step")))
    else:
        _step_note = ""
    await _settle(page, timeout=25000)
    await _dismiss_overlays(page)

    # The payment section is drawn after the step transition, so give it time to
    # exist before deciding it does not.
    try:
        await page.locator(
            "input[type=radio][name*='payment'], input[type=radio][value*='_'], "
            ".payment-method, [id*='payment-method']"
        ).first.wait_for(state="visible", timeout=30000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)

    # Now choose the card method itself.
    picked = False
    _last = method.split("_")[-1]
    for sel in ("input[value='%s']" % method,
                "#%s" % method,
                "input[id*='%s']" % _last,
                "input[value*='%s']" % _last,
                "label:has-text('Credit')", "label:has-text('Debit')",
                "label:has-text('Card')"):
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.scroll_into_view_if_needed(timeout=5000)
                await el.click(timeout=8000, force=True)
                picked = True
                break
        except Exception:
            continue
    await page.wait_for_timeout(2500)

    if not picked:
        # Ask the page what payment options it HAS. Three selectors in a row have
        # now been guessed from assumed markup; the page can just answer.
        try:
            _opts = await page.eval_on_selector_all(
                "input[type=radio], .payment-method, [data-role*='payment']",
                """els => els.filter(e => e.offsetParent).map(e => {
                     const v = e.getAttribute('value') || '';
                     const i = e.id || '';
                     const t = (e.closest('label,.payment-method')?.innerText || '')
                                 .trim().split('\n')[0];
                     return [v, i, t].filter(Boolean).join(' | ');
                   }).filter(Boolean).slice(0, 12)""")
        except Exception:
            _opts = []
        _pay_note = ("payment options on the page: %s%s"
                     % (" ;; ".join(_opts) or "none visible",
                        await _capture(page,
                                       (cfg.get("row") or {}).get("username") or "user",
                                       "payment-step")))
    else:
        _pay_note = ""
    if picked:
        return "address: %s%s" % (filled or "none needed",
                                  ("; " + _step_note) if _step_note else "")
    if _step_note:
        # The method being unselectable is a SYMPTOM when the page never left
        # the delivery step -- report the cause, not the thing downstream of it.
        return "%s (so the payment section never rendered)" % _step_note
    return ("payment method %r was not selectable. %s"
            % (method, _pay_note))


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


async def _empty_cart(page, cfg: dict) -> str:
    """Empty the basket before adding anything.

    Every attempt adds a product, and a failed attempt leaves it there. After
    several runs the basket held thousands of pounds of stock and checkout
    stopped rendering altogether -- a spinner and nothing else. That is not a
    store fault, it is our own litter.

    It also makes each account's run start from a known state, which is the
    only way the captured token means what we think it means.
    """
    try:
        await page.goto(cfg["cart_url"], wait_until="domcontentloaded", timeout=45000)
        await _dismiss_overlays(page)
        await _settle(page, timeout=25000)
    except Exception as exc:
        return "cart page unreachable: %s" % _redact(exc)[:60]

    removed = 0
    for _ in range(40):                       # bounded: never loop on a page that will not empty
        try:
            btn = page.locator("a.action-delete, .action.action-delete, "
                               "a[title='Remove item']").first
            if not await btn.count() or not await btn.is_visible():
                break
            await btn.click(timeout=8000)
            await page.wait_for_timeout(2200)
            removed += 1
        except Exception:
            break
    return "emptied %d item(s)" % removed if removed else ""


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
    await _empty_cart(page, cfg)

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


async def _capture(page, user: str, step: str) -> str:
    """Photograph the page and list what is on it, at the moment it failed.

    Four selectors in a row have now been guessed from markup I assumed rather
    than read -- the card fields, Place Order, Proceed To Payment, and the
    payment-method control -- and each guess cost a full run against a slow
    store. A screenshot and an element dump cost one second and end that loop:
    the next fix is made from what the page IS, not from what it ought to be.
    """
    stamp = re.sub(r"[^A-Za-z0-9]+", "-", "%s-%s" % (user.split("@")[0], step))[:60]
    base = Path("enrol-failures")
    try:
        base.mkdir(exist_ok=True)
        shot = base / ("%s.png" % stamp)
        await page.screenshot(path=str(shot), full_page=True)
        dump = base / ("%s.txt" % stamp)
        info = await page.evaluate(
            """() => {
                 const vis = e => e.offsetParent !== null;
                 const txt = e => (e.innerText || e.value || '').trim()
                                    .replace(/[\\s]+/g, ' ').slice(0, 60);
                 const desc = e => [e.tagName.toLowerCase(),
                                    e.id && ('#' + e.id),
                                    e.getAttribute('name') && ('[name=' + e.getAttribute('name') + ']'),
                                    e.getAttribute('value') && ('[value=' + e.getAttribute('value') + ']'),
                                    e.disabled ? '(disabled)' : '',
                                    txt(e) && ('"' + txt(e) + '"')]
                                   .filter(Boolean).join(' ');
                 return {
                   url: location.href,
                   buttons: [...document.querySelectorAll('button, input[type=submit], a.action')]
                              .filter(vis).map(desc).slice(0, 30),
                   inputs: [...document.querySelectorAll('input, select')]
                              .filter(vis).map(desc).slice(0, 40),
                   errors: [...document.querySelectorAll(
                              '.mage-error, .message-error, [class*=error]')]
                              .filter(vis).map(txt).filter(Boolean).slice(0, 12),
                   frames: [...document.querySelectorAll('iframe')]
                              .map(f => f.src || '(no src)').slice(0, 10),
                 };
               }"""
        )
        with io.open(dump, "w", encoding="utf-8") as fh:
            fh.write("url: %s\n\nBUTTONS\n" % info.get("url"))
            for b in info.get("buttons") or []:
                fh.write("  %s\n" % b)
            fh.write("\nINPUTS\n")
            for i in info.get("inputs") or []:
                fh.write("  %s\n" % i)
            fh.write("\nVALIDATION ERRORS ON THE PAGE\n")
            for e in info.get("errors") or ["(none)"]:
                fh.write("  %s\n" % e)
            fh.write("\nIFRAMES\n")
            for f in info.get("frames") or ["(none)"]:
                fh.write("  %s\n" % f)
        errs = "; ".join((info.get("errors") or [])[:3])
        return (" [captured %s | errors on page: %s]"
                % (shot, errs or "none"))
    except Exception as exc:
        return " [could not capture the page: %s]" % _redact(exc)[:60]


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

        step = "payment step"
        note = await _reach_payment_step(page, cfg, cfg["method"])

        step = "card form"
        frame = await _card_frame(page, cfg["frame_patterns"])
        if frame is not None:
            # Before a single character of the card is typed.
            _assert_frame_allowed(frame.url, cfg["guards"])
        if frame is None:
            urls = ", ".join(sorted({(f.url or "").split("?")[0][:70]
                                     for f in page.frames if f.url})) or "none"
            return None, ("card form: no gateway frame appeared. %s. Checkout is at "
                          "%s and the page holds these frames: %s"
                          % (note or "payment method selected",
                             (page.url or "?").split("?")[0], urls)
                  + await _capture(page, user, 'no-frame'))

        card = cfg["card"]
        # Documented selectors first, then whatever the real form actually has.
        # The published ones did not match this store, so discovery is not a
        # fallback -- it is what makes the script work on a form nobody has
        # written selectors for.
        discovered = await _discover_card_fields(frame)
        # Log this on EVERY attempt, not only on failure. The operator watched
        # the card form go unfilled, typed it by hand, and the run then finished
        # -- so it succeeded and left no capture, and the reason it could not
        # fill the form went with it. A rescued run is still a run that did not
        # work unattended, and it has to say so.
        _seen_now = discovered.get("_seen") or []
        print("      card form fields: %s"
              % (", ".join((f.get("name") or f.get("id") or "?")
                           for f in _seen_now[:12]) or "none visible"), flush=True)
        print("      matched: %s"
              % (", ".join("%s->%s" % (k, v) for k, v in discovered.items()
                           if k != "_seen") or "nothing"), flush=True)
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
                return None, ("card form: no %s field found. The form at %s has: %s. "
                              "Earlier: %s"
                              % (field, (frame.url or "?").split("?")[0], names,
                                 note or "?")
                      + await _capture(page, user, 'card-fields'))

        step = "place order"
        if not await _click_when_ready(page, cfg["sel"]["place_order"], timeout=25000):
            labels = await page.eval_on_selector_all(
                "button, input[type=submit]",
                "els => els.filter(e => e.offsetParent).map("
                "e => (e.innerText || e.value || '').trim()).filter(Boolean).slice(0, 14)")
            return None, ("place order: no button matched. Visible buttons: %s"
                          % (", ".join(labels) or "none")
                  + await _capture(page, user, 'place-order'))
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
        note = await _capture(page, user, step)
        return None, "%s: %s%s" % (step, _redact(exc)[:180], note)
    finally:
        await ctx.close()


async def run(args) -> int:
    kb = _load_kb()
    prof = _gateway_profile(kb, args.gateway)
    if not prof:
        raise SystemExit("no knowledge-base profile matches gateway %r" % args.gateway)
    card = _check_safeguards(prof, args.base_url, args.allow_unknown_card, args.card)
    if args.cvv:
        # Overriding the security code is safe in a way that overriding the
        # NUMBER is not: on a sandbox card any 3 digits are accepted, and a
        # wrong one is a decline to test, not a way to reach a real account.
        if not args.cvv.isdigit() or not 3 <= len(args.cvv) <= 4:
            raise SystemExit("--cvv must be 3 or 4 digits")
        card = dict(card)
        card["cvc"] = args.cvv
    guards = prof.get("safeguards") or {}

    src = Path(args.csv)
    rows = list(csv.DictReader(io.open(src, encoding="utf-8-sig")))
    if not rows:
        raise SystemExit("%s has no rows" % src)

    hosted = [str(h).lower() for h in (args.hosted or "cybersource,paradoxlabs,stripe,"
                                       "braintree,adyen,authorizenet").split(",")]
    todo, seen, cannot = [], set(), []
    for r in rows:
        u = (r.get("username") or "").strip().lower()
        if not u or u in seen:
            continue
        seen.add(u)
        action, reason = _classify_row(r, hosted, kb)
        if action == "enrol":
            todo.append(r)
        elif action == "cannot":
            cannot.append(((r.get("username") or "").strip(), reason))

    print("using the %r sandbox card (ending %s) from the knowledge base%s"
          % (args.card, str(card.get("number", ""))[-4:],
             " with cvv override" if args.cvv else ""))
    print("%d row(s), %d account(s) needing a token" % (len(rows), len(todo)))
    for user, reason in cannot:
        # Never silent. A row that declared a gateway and got no attempt has to
        # say why, or the run reads as "all handled" when it was not.
        print("  SKIPPED %-28s %s" % (user, reason))
    if not todo:
        print("nothing to enrol" if cannot
              else "nothing to do -- every card account already carries one")
        return 0 if not cannot else 1

    base = args.base_url.rstrip("/")
    cfg = {
        "login_url": base + "/customer/account/login/",
        "checkout_url": base + "/checkout/",
        "cart_url": base + "/checkout/cart/",
        "settle_ms": args.settle_ms,
        "frame_patterns": prof.get("frame_url_patterns") or [],
        "card_sel": _card_selectors(prof),
        "card": card,
        "guards": guards,
        "method": args.gateway_code or "",
        "row": {},
        "company": args.company,
        "search_url": lambda term: base + "/catalogsearch/result/?q=%s" % term,
        "product_url": lambda pid: base + "/catalog/product/view/id/%s" % pid,
        "sel": {
            "login_user": "input[name='login[username]'], input#email, input[name=email]",
            "login_pass": "input[name='login[password]'], input#pass, input[name=password]",
            "login_submit": "button#send2, button[type=submit]",
            "qty": "input#qty, input[name=qty]",
            "add_to_cart": "button#product-addtocart-button, button.tocart, "
                           "button:has-text('Add to Cart'), "
                           "button:has-text('Add to Basket'), "
                           "button[title*='Add to Cart' i], "
                           "button[title*='Add to Basket' i]",
            "result_link": "a.product-item-link, .product-item-info a.product, "
                           "li.product-item a.product-item-photo",
            "ship_method": ".table-checkout-shipping-method input[type=radio], "
                           "input[name='ko_unique_1'], "
                           "#checkout-shipping-method-load input[type=radio]",
            # Every wording a checkout uses to mean "on to the next step". The
            # first live run stalled on a button reading "Proceed To Payment",
            # which matched none of Next/Continue -- the two words I had assumed
            # were the vocabulary. Text first, attributes behind.
            "continue": "button:has-text('Proceed To Payment'), "
                        "button:has-text('Proceed to Payment'), "
                        "button:has-text('Continue to Payment'), "
                        "button:has-text('Go to Payment'), "
                        "button:has-text('Proceed'), "
                        "button:has-text('Next'), button:has-text('Continue'), "
                        "button[data-role='opc-continue'], button.continue, "
                        "button.button.action.continue, "
                        "button[title*='Next' i], button[title*='Continue' i], "
                        "button[title*='Proceed' i]",
            # Matched by the words on the button. The title attribute is a
            # theme detail and this theme does not set it -- the last run timed
            # out on button[title*='Place Order'] while a button reading
            # "Place Order" was on screen.
            "place_order": "button:has-text('Place Order'), "
                           "button:has-text('Place order'), "
                           "button.action.primary.checkout, "
                           "button[title*='Place Order' i], "
                           "button[data-role='review-save'], button.checkout",
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
                cfg["method"] = ((row.get("payment_method") or "").strip()
                                 or cfg["method"])
                cfg["row"] = row
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
    p.add_argument("--cvv", default="",
                   help="override the security code on the chosen card "
                        "(3-4 digits; use a wrong one to test a decline)")
    p.add_argument("--company", default="APEA Load Test",
                   help="company name, when the checkout requires one and the "
                        "data file has no company column")
    p.add_argument("--gateway-code", default="",
                   help="payment method code to select at checkout when the row "
                        "does not name one")
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
