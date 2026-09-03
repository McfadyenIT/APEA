"""LLM-assisted, platform-agnostic browser-journey planner (Track B).

Turns a parsed recording of ANY application (not one hard-coded site) into a
concrete plan the Playwright runner uses to REACH and COMPLETE a hosted-iframe
card payment: how to log in, how to get to the checkout page, WHICH payment
gateway is in play, and the pay / submit / success signals.

Why this exists: hosted card fields live in a cross-origin iframe that HTTP
replay can't drive, and the selectors/flow differ per app. Rather than hard-code
per site, LT Metrics (a) detects the gateway from the recording deterministically and
(b) uses Claude — the reason the LLM layer is integrated — to infer the checkout
selectors/URLs for the detected platform. Both are grounded in the recording.

Design rules (same as llm.py): build-time only, never on the load hot path,
never raises. Deterministic heuristics ALWAYS produce a usable plan; Claude only
ENRICHES it when ANTHROPIC_API_KEY is set. With no key, it degrades to the
heuristics — the browser track still runs, just with less-precise selectors.
"""
from __future__ import annotations

import re

# Generic gateway signatures — matched against recorded hosts/paths/bodies AND
# the cart's payment-method codes. Add a row to teach LT Metrics a new gateway; nothing
# here is tied to a particular merchant.
_GATEWAY_SIGNATURES = {
    "stripe": ["js.stripe.com", "hooks.stripe.com", "stripe"],
    "paradoxlabs_cybersource": ["secureacceptance", "secureaccept", "flex.cybersource",
                                "cybersource", "paradoxlabs"],
    "adyen": ["adyen", "checkoutshopper"],
    "braintree": ["braintree"],
    "paypal": ["paypal"],
}

# Human labels for the UI (generic — not tied to any merchant).
GATEWAY_LABELS = {
    "stripe": "Stripe",
    "paradoxlabs_cybersource": "CyberSource (ParadoxLabs)",
    "adyen": "Adyen",
    "braintree": "Braintree",
    "paypal": "PayPal",
}

# Paths that look like a checkout step but are NOT a navigable checkout page
# (session/currency setters, AJAX section loads, gateway param calls, and — the
# big one — /static/ & /media/ JS TEMPLATE files whose path merely CONTAINS
# "checkout" e.g. .../Magento_Captcha/template/checkout/captcha.html). Excluded
# from nav + checkout-URL detection so we don't send the browser to a dead asset.
_JUNK_PATH = ("setsession", "setcurrency", "section/load", "getparams",
              "/ajax", "estimate-shipping", "shipping-information",
              "payment-information", "/rest/", "/graphql", "/static/", "/media/")

# A page URL that ends in an asset/file extension is a FILE, not a navigable
# checkout page (guards against a stray template slipping through _JUNK_PATH).
_ASSET_EXT_RE = re.compile(
    r"\.(html?|js|mjs|css|json|png|jpe?g|svg|gif|webp|ico|woff2?|ttf|map)(\?|$)", re.I)

# Platform-agnostic URL-substring signals for the milestones we care about.
_LOGIN_SIG = ("account/login", "loginpost", "customer/ajax/login", "/login",
              "signin", "sign-in", "/session")
_CART_SIG = ("cart/add", "carts/mine/items", "add-to-cart", "add_to_cart",
             "checkout/cart", "/cart")
_CHECKOUT_SIG = ("/checkout", "onepage", "shipping-information",
                 "estimate-shipping", "payment-information", "/payment")
_SUCCESS_SIG = ("onepage/success", "checkout/success", "order-received",
                "thank-you", "thankyou", "order-confirmation", "/success")
_LOGIN_URL_OK = ("login", "signin", "sign-in", "auth", "/session")
_PRODUCT_SIG = ("/product/", "/buy/", "catalog/product/view", "/p/")


# ------------------------------------------------------------------ #
# Extract the REAL payment flow from the recording's Selenium ui_steps
# (the exact iframe + field selectors the tester actually used), so the
# browser track replays them instead of guessing generic KB selectors.
# ------------------------------------------------------------------ #
def _parse_verb(verb: str):
    """('typeByID'|'selectByCSS'|'switchFrame'...) -> (action, how)."""
    m = re.match(r"^(type|select|click|waitFor|switchFrame)(?:By(ID|CSS|Name|XPath|LinkText))?",
                 str(verb or ""))
    if not m:
        return (None, None)
    action = {"type": "type", "select": "select", "click": "click",
              "waitFor": "wait", "switchFrame": "frame"}[m.group(1)]
    return (action, (m.group(2) or "").lower())


def _sel_from(how: str, target: str):
    """A Selenium (how, target) -> a Playwright CSS/engine selector."""
    t = str(target or "").split(",")[0].strip()      # drop ",Clickable" suffixes
    if not t:
        return None
    if how == "id":
        return '[id="%s"]' % t
    if how == "name":
        return '[name="%s"]' % t
    if how == "xpath":
        return "xpath=" + t
    if how == "linktext":
        return "text=" + t
    # css / unknown — a BARE identifier (no CSS syntax) is almost always an
    # element id the recorder emitted without '#' (e.g. 'pass', 'login-password').
    if re.match(r"^[A-Za-z][\w\-]*$", t):
        return '[id="%s"]' % t
    return t                                          # real CSS


def _frame_sel(target: str):
    t = str(target or "").strip()
    for pfx, fmt in (("id=", '[id="%s"]'), ("name=", '[name="%s"]'),
                     ("css=", "%s"), ("xpath=", "xpath=%s")):
        if t.startswith(pfx):
            return fmt % t[len(pfx):]
    return t


_ROLE_RULES = [
    ("number", ("card_number", "cardnumber", "cc-number", "pan")),
    ("cvc", ("card_cvn", "cvn", "cvc", "cvv", "cc-cid", "securitycode", "security_code")),
    ("exp_month", ("expiry_month", "exp_month", "expmonth", "expirationmonth")),
    ("exp_year", ("expiry_year", "exp_year", "expyear", "expirationyear")),
    ("expiry", ("card_expiry", "exp-date", "exp_date", "expiration", "expiry")),
    ("postal", ("postal", "postcode", "zip", "zipcode")),
    ("password", ("password", "passwd", "pwd")),
    ("username", ("username", "userid", "user_id", "j_username", "login[username]")),
    ("firstname", ("forename", "firstname", "first_name", "fname", "givenname")),
    ("lastname", ("surname", "lastname", "last_name", "lname", "familyname")),
    ("country", ("country",)),
    ("state", ("state", "region", "province")),
    ("city", ("city", "town")),
    ("street", ("street", "address_line", "addressline", "address1", "bill_to_address_line")),
    ("email", ("email",)),
    ("phone", ("phone", "telephone", "mobile")),
]
_CARD_ROLES = {"number", "cvc", "exp_month", "exp_year", "expiry", "postal"}

# Verbs / targets that are recorder instrumentation, not real user actions.
_NAV_VERBS = ("open", "get", "go", "navigate", "gotourl", "windowopen", "url")
_NOISE_TARGET = ("scribe-recorder-ready", "data-scribe")
_GENERIC_CLICK = ("main", "body", "html", "#main")


# Click-intent keywords (most specific first) — used to pick a KB resilience
# fallback when a recorded click selector fails. Platform-agnostic.
_INTENT_RULES = [
    ("place_order", ("placeorder", "place-order", "place_order", "submitorder", "revieworder")),
    ("proceed_payment", ("proceed to payment", "proceedtopayment", "to-payment",
                         "review & payment", "review-payment", "payment-continue")),
    ("checkout", ("checkout-link", "checkout_link", "proceed to checkout", "tocheckout", "checkout")),
    ("add_to_cart", ("addtocart", "add-to-cart", "add_to_cart", "tocart", "add to cart", "add to basket")),
    ("signin", ("sign.in", "sign-in", "signin", "sign in", "account.login", "customer.header.sign")),
    ("product", ("product", "item-link", "item-info", "result", "img", "card", "tile", "thumbnail")),
]


def _click_intent(target, label) -> str | None:
    blob = (str(target or "") + " " + str(label or "")).lower()
    for intent, keys in _INTENT_RULES:
        if any(k in blob for k in keys):
            return intent
    return None


def build_ui_journey(ui_steps) -> list:
    """Turn the recording's Selenium steps into an ORDERED list of browser actions
    the runner replays verbatim — the WHOLE business flow (browse, search, add to
    cart, checkout, shipping, payment), for any app. This is LT Metrics's core idea:
    understand the recorded flow and reproduce it, rather than guessing per site.

    Actions: {navigate|click|type|select|frame|frame_reset}. Card fields carry
    role only (value resolved to the sandbox test card at runtime); credentials
    carry role (resolved to the run's creds); everything else keeps the recorded
    value. Recorder-instrumentation steps and bare readiness waits are dropped.
    Never raises; returns [] when the recording has no usable UI steps."""
    try:
        out = []
        for s in (ui_steps or []):
            verb = str(s.get("verb") or "")
            low_verb = verb.lower()
            target = s.get("target")
            value = s.get("value")
            tlow = str(target or "").lower()
            if any(n in tlow for n in _NOISE_TARGET):
                continue
            if any(low_verb.startswith(nv) for nv in _NAV_VERBS):
                url = str(value or target or "").strip()
                if url and not url.lower().startswith("javascript"):
                    out.append({"action": "navigate", "url": url})
                continue
            action, how = _parse_verb(verb)
            if action in (None, "wait"):
                continue                                  # rely on Playwright auto-wait
            if action == "frame":
                t = tlow.strip()
                if (not t) or "parent" in t or "default" in t or "top" in t:
                    out.append({"action": "frame_reset"})
                else:
                    out.append({"action": "frame", "sel": _frame_sel(target)})
                continue
            sel = _sel_from(how, target)
            if not sel:
                continue
            if action == "click":
                if str(target or "").split(",")[0].strip().lower() in _GENERIC_CLICK:
                    continue
                click = {"action": "click", "sel": sel}
                intent = _click_intent(target, s.get("step_label"))
                if intent:
                    click["intent"] = intent       # KB resilience fallback key
                out.append(click)
            else:                                         # type / select
                role = _role_of(target)
                out.append({"action": action, "sel": sel, "role": role,
                            "value": None if role in _CARD_ROLES else value})
        return out[:400]
    except Exception:
        return []


def _role_of(target: str):
    t = str(target or "").lower()
    for role, keys in _ROLE_RULES:
        if any(k in t for k in keys):
            return role
    return "other"


def extract_payment_steps(ui_steps) -> dict | None:
    """Turn the recording's Selenium payment steps into a concrete plan the
    browser runner can replay: the iframe to switch into, the card/billing fields
    (with type vs select), the reveal/agreement/submit clicks. Returns None if the
    recording has no recognisable card entry (caller falls back to KB selectors).
    Never raises."""
    try:
        steps = ui_steps or []
        iframe = None
        pay_trigger = None
        agreement = None
        submit = None
        fields, seen = [], set()
        for s in steps:
            action, how = _parse_verb(s.get("verb"))
            target = s.get("target")
            if action == "frame":
                fs = _frame_sel(target)
                low = str(target or "").lower()
                if fs and (iframe is None or any(g in low for g in
                           ("cybersource", "secureaccept", "payment", "card", "adyen",
                            "stripe", "braintree", "iframe"))):
                    iframe = fs
            elif action in ("type", "select"):
                role = _role_of(target)
                if role == "other":
                    continue                          # skip unrelated inputs
                sel = _sel_from(how, target)
                if not sel or sel in seen:
                    continue
                seen.add(sel)
                fields.append({"sel": sel, "action": action, "role": role,
                               "value": s.get("value") if role not in _CARD_ROLES else None})
            elif action == "click":
                low = str(target or "").lower()
                lbl = str(s.get("step_label") or "").lower()
                sel = _sel_from(how, target)
                if "agreement" in low:
                    agreement = agreement or sel
                elif ("proceed" in low or "proceed" in lbl or "to payment" in low
                      or "to payment" in lbl):
                    pay_trigger = pay_trigger or sel
                elif any(k in low for k in ("placeorder", "place-order", "place_order",
                                            "submitorder", "revieworder")) or \
                        any(k in lbl for k in ("place order", "place-order")):
                    submit = sel                      # last such wins
        # only a real card entry counts as a usable payment extraction
        if not any(f["role"] == "number" for f in fields):
            return None
        return {"iframe": iframe, "pay_trigger": pay_trigger, "agreement": agreement,
                "submit": submit, "fields": fields}
    except Exception:
        return None


def _ai_payment_steps(ui_steps) -> dict | None:
    """Claude fallback: when the deterministic classifier can't recognise the
    payment steps (unusual verbs/ids), have Claude read the recorded Selenium UI
    steps and identify the card-payment flow — this is the 'AI understands the
    business flow from the recording' path, and it's platform-agnostic. Returns
    the same plan shape, or None (no key / no card flow / failure)."""
    try:
        from . import llm
        if not llm.available() or not ui_steps:
            return None
        import json
        labels = [str(s.get("step_label") or (str(s.get("verb")) + "(" + str(s.get("target")) + ")"))
                  for s in ui_steps][:250]
        prompt = (
            "These are recorded browser (Selenium) UI steps from a checkout. "
            "Identify ONLY the credit/debit CARD payment portion and return how a "
            "headless browser would replay it. Use the exact element ids/selectors "
            "shown in the steps. If there is no card payment, return {}.\n\n"
            "STEPS:\n" + json.dumps(labels) + "\n\n"
            "Return ONLY JSON:\n"
            '{"iframe":"CSS selector for the payment iframe ELEMENT (or null if fields are on the main page)",'
            '"pay_trigger":"CSS to reveal the card form (e.g. a Proceed-to-Payment button, or null)",'
            '"agreement":"CSS for a required T&C checkbox (or null)",'
            '"submit":"CSS for the Place-order / Pay button (or null)",'
            '"fields":[{"sel":"CSS selector","action":"type|select",'
            '"role":"number|cvc|exp_month|exp_year|expiry|postal|firstname|lastname|country|state|street|city|email|phone|other"}]}')
        _dk = llm.cache_key("aipaysteps", json.dumps(labels))
        out = llm.cache_get("aipaysteps", _dk)
        if out is None:
            out = llm.json_call(
                prompt,
                system=("You convert recorded checkout UI steps into a browser payment "
                        "plan. Output only strict JSON; use the real selectors from the "
                        "steps; never fabricate."),
                max_tokens=900) or {}
            llm.cache_put("aipaysteps", _dk, out)
        fields = [f for f in (out.get("fields") or [])
                  if isinstance(f, dict) and f.get("sel") and f.get("role")]
        if not any(f.get("role") == "number" for f in fields):
            return None
        for f in fields:
            f["action"] = "select" if str(f.get("action")) == "select" else "type"
            if f.get("role") in _CARD_ROLES:
                f["value"] = None
        return {"iframe": out.get("iframe") or None,
                "pay_trigger": out.get("pay_trigger") or None,
                "agreement": out.get("agreement") or None,
                "submit": out.get("submit") or None,
                "fields": fields}
    except Exception:
        return None


def detect_gateway(discovery: dict, payment_codes=None) -> str | None:
    """Best-effort gateway id from the recording (hosts/paths/bodies) + the cart's
    available payment codes. Returns a KB gateway key or None. Never raises."""
    try:
        hay = []
        for s in (discovery.get("flow") or []):
            hay.append(str(s.get("path") or "").lower())
            b = s.get("body")
            if isinstance(b, str):
                hay.append(b.lower())
        for c in (payment_codes or []):
            hay.append(str(c).lower())
        blob = " ".join(hay)
        for gw, sigs in _GATEWAY_SIGNATURES.items():
            if any(sig in blob for sig in sigs):
                return gw
    except Exception:
        pass
    return None


def _first_path(flow, sigs):
    for s in flow:
        if (s.get("method") or "GET").upper() != "GET":
            continue                       # a checkout PAGE is a GET, not a POST
        p = (s.get("path") or "").lower()
        if any(j in p for j in _JUNK_PATH):
            continue                       # skip session/ajax/gateway-param junk
        if any(sig in p for sig in sigs):
            return s.get("path")
    return None


def _store_code(flow) -> str | None:
    """The storefront store-code path segment straight from the recording's REST
    URLs (e.g. '/uk/rest/uk/V1/...' -> 'uk'). This is recording-derived, so the
    checkout/login pages we build are the app's REAL pages, not a guess."""
    for s in (flow or []):
        m = re.search(r"^/([A-Za-z0-9_-]+)/rest/", str(s.get("path") or ""))
        if m:
            return m.group(1)
    return None


def _derive_checkout_url(flow, store) -> str:
    """Prefer the EXACT checkout page the recording navigated (a real GET, not a
    REST/session/ajax call). If the recording only has REST checkout calls (no
    browsable page), build the platform-standard checkout page from the recorded
    store code — still recording-derived, never a blind guess."""
    for s in (flow or []):
        if (s.get("method") or "GET").upper() != "GET":
            continue
        p = str(s.get("path") or "")
        low = p.lower()
        if any(j in low for j in _JUNK_PATH):
            continue
        if _ASSET_EXT_RE.search(low):                   # a file (.html/.js/…), not a page
            continue
        if "/checkout" in low and "/v1/" not in low:   # a real checkout PAGE
            return p
    return ("/%s/checkout" % store) if store else "/checkout"


def _search_term(ui_steps) -> str | None:
    """The value the tester typed into the site search box (recording-derived), so
    the browser track can reach a real product via search results."""
    for s in (ui_steps or []):
        action, _how = _parse_verb(s.get("verb"))
        if action == "type":
            t = str(s.get("target") or "").lower()
            if "search" in t or t in ("q", "[id=\"q\"]", "[name=\"q\"]") or "=q" in t:
                v = s.get("value")
                if v and str(v).strip():
                    return str(v).strip()
    return None


def _product_url(flow) -> str | None:
    """A product-detail page the recording visited — so the browser track can open
    a PDP and read the REAL (client-rendered) price that HTTP replay sees as 0.
    Matches a `<digits>.html` PDP slug or a /product//buy/ path."""
    for s in (flow or []):
        if (s.get("method") or "GET").upper() != "GET":
            continue
        p = str(s.get("path") or "")
        low = p.lower()
        if any(j in low for j in _JUNK_PATH):
            continue
        if re.search(r"/\d{4,}\.html($|\?)", low) or any(sig in low for sig in _PRODUCT_SIG):
            return p
    return None


def _nav_to_checkout(flow) -> list:
    """Ordered, de-duplicated GET pages that walk a shopper toward checkout
    (login → cart → checkout). Skips API/asset paths. Falls back to '/'."""
    steps, seen = [], set()

    def add(p):
        p = (p or "").strip()
        low = p.lower()
        if not p or p in seen:
            return
        if any(k in low for k in ("/rest/", "/api/", "/graphql", "/ajax", "/rpc")):
            return
        if any(j in low for j in _JUNK_PATH):     # session/ajax/gateway-param junk
            return
        seen.add(p)
        steps.append({"label": p[:60], "path": p})

    for s in flow:
        if (s.get("method") or "GET").upper() != "GET":
            continue
        low = (s.get("path") or "").lower()
        if any(k in low for k in _LOGIN_SIG + _CART_SIG + _CHECKOUT_SIG):
            add(s.get("path"))
    if not steps:                       # no obvious milestone GETs — take some pages
        for s in flow:
            if (s.get("method") or "GET").upper() == "GET":
                add(s.get("path"))
            if len(steps) >= 6:
                break
    if not steps:
        add("/")
    return steps[:10]


def plan_journey(discovery: dict, plan_cfg: dict | None = None,
                 payment_codes=None) -> dict:
    """Deterministic plan (+ optional Claude enrichment). Never raises."""
    discovery = discovery or {}
    flow = discovery.get("flow") or []
    login_form = discovery.get("login_form") or {}
    store = _store_code(flow)
    prod = _product_url(flow)
    nav = _nav_to_checkout(flow)
    if prod and prod not in [s.get("path") for s in nav]:
        nav = [{"label": prod[:60], "path": prod}] + nav   # open a PDP first (price)
    plan = {
        "gateway": detect_gateway(discovery, payment_codes),
        "nav": nav,
        "product_url": prod,
        "login": None,
        # the EXACT checkout page from the recording (or the store's real checkout
        # page derived from the recorded store code) — not a signature guess
        "checkout_url": _derive_checkout_url(flow, store),
        "add_to_cart_selector": None,      # optional hint (runner has heuristics)
        "payment_method_selector": None,   # radio that reveals the card gateway
        "pay_trigger_selector": None,
        "submit_selector": None,
        "success_url_contains": next(
            (sig for sig in _SUCCESS_SIG
             if any(sig in (s.get("path") or "").lower() for s in flow)), None),
    }
    # login URL, recording-derived: a real login form action if crawled, else the
    # platform-standard login page for the recorded store code. Set the URL
    # deterministically so the LLM only fills selectors (never overrides the URL
    # with a wrong page, which is what produced the bogus category-page login).
    _login_url = login_form.get("action")
    # Don't trust a login_form.action that isn't actually a login page (a stale /
    # misclassified crawl once gave a category page here) — derive the standard
    # login page from the recorded store code instead.
    if _login_url and not any(k in _login_url.lower() for k in _LOGIN_URL_OK):
        _login_url = None
    if not _login_url:
        _login_url = ("/%s/customer/account/login" % store) if store else "/customer/account/login"
    plan["login"] = {"url": _login_url, "username_selector": None,
                     "password_selector": None, "submit_selector": None}
    # the REAL payment selectors from the recording's Selenium steps — replayed
    # faithfully by the runner. Deterministic classifier first; if it can't
    # recognise the steps, let Claude read them (AI understands the business flow);
    # KB selectors remain the last-resort fallback in the runner.
    _ui = discovery.get("ui_steps")
    # Full recorded browser journey (browse -> cart -> checkout -> pay), replayed
    # verbatim — platform-agnostic because it IS the recorded flow, not a guess.
    plan["ui_journey"] = build_ui_journey(_ui)
    # Direct-to-PDP: reach a REAL product robustly via the store's search results
    # for the recorded search term, then read the first result's href (instead of
    # the fragile 'click the product tile' step). Platform-standard search path.
    _term = _search_term(_ui)
    if _term:
        from urllib.parse import quote
        _su = ("/%s/catalogsearch/result/?q=%s" % (store, quote(_term))) if store \
            else ("/catalogsearch/result/?q=%s" % quote(_term))
        plan["product_search"] = {"term": _term, "results_url": _su}
    else:
        plan["product_search"] = {}
    plan["recorded_payment"] = extract_payment_steps(_ui) or _ai_payment_steps(_ui)
    _enrich_with_llm(plan, discovery)
    return plan


def _enrich_with_llm(plan: dict, discovery: dict) -> None:
    """Ask Claude to infer checkout selectors/URLs from the recording + platform.
    Grounded, JSON-only, guarded — leaves `plan` unchanged with no API key or on
    any failure. Fills ONLY the fields the deterministic pass left empty."""
    try:
        from . import llm
        if not llm.available():
            return
        import json
        flow = discovery.get("flow") or []
        listing = [{"m": (s.get("method") or "GET"), "p": (s.get("path") or "")[:120]}
                   for s in flow[:60]]
        tech = discovery.get("tech") or []
        ai = discovery.get("ai_analysis") or {}
        platform = ai.get("platform") or (tech[0] if tech else "unknown")
        prompt = (
            "You are configuring a REAL-BROWSER checkout automation for load "
            "testing. From this recorded HTTP journey and the platform, infer the "
            "CSS selectors and URL signals a headless browser needs to reach and "
            "submit the hosted-iframe card payment. Prefer selectors that are "
            "STANDARD for the detected platform; do NOT invent site-specific ids "
            "you cannot justify. Use null when unsure.\n\n"
            "PLATFORM: " + str(platform) + "\n"
            "DETECTED GATEWAY: " + str(plan.get("gateway")) + "\n"
            "RECORDED STEPS (method, path):\n" + json.dumps(listing) + "\n\n"
            "Return ONLY JSON with these keys (any may be null):\n"
            '{"checkout_url":"path to the checkout/payment page",'
            '"add_to_cart_selector":"CSS for the Add-to-Cart / Add-to-Basket button on the product page",'
            '"payment_method_selector":"CSS for the radio/option that selects the CARD payment method (reveals its iframe)",'
            '"pay_trigger_selector":"CSS to reveal the card form if it is behind a step/tab",'
            '"submit_selector":"CSS for the Pay / Place-order button",'
            '"success_url_contains":"a fragment of the order-confirmation URL",'
            '"login":{"url":"login path","username_selector":"CSS","password_selector":"CSS","submit_selector":"CSS"}}')
        _dk = llm.cache_key("bjenrich", platform, plan.get("gateway"), json.dumps(listing))
        out = llm.cache_get("bjenrich", _dk)
        if out is None:
            out = llm.json_call(
                prompt,
                system=("You output only strict JSON to drive browser checkout "
                        "automation. Prefer platform-standard selectors; never fabricate."),
                max_tokens=700) or {}
            llm.cache_put("bjenrich", _dk, out)
        for k in ("checkout_url", "add_to_cart_selector", "payment_method_selector",
                  "pay_trigger_selector", "submit_selector", "success_url_contains"):
            v = out.get(k)
            if isinstance(v, str) and v.strip() and not plan.get(k):
                plan[k] = v.strip()
        lg = out.get("login")
        if isinstance(lg, dict):
            base = dict(plan.get("login") or {})
            for k in ("url", "username_selector", "password_selector", "submit_selector"):
                if isinstance(lg.get(k), str) and lg[k].strip() and not base.get(k):
                    base[k] = lg[k].strip()
            if base:
                plan["login"] = base
    except Exception:
        return
