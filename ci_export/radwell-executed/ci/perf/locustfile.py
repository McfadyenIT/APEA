"""
APEA-generated Locust script (recorded checkout flow).
Target : https://mcstaging.radwell.eu
Domain : Content / Media
Profile: Smoke Test (1 users, 4m)
Steps  : 100 recorded transactions, replayed sequentially in one session
         with fresh form_key correlation and CSV-driven login.
"""
import csv
import json
import os
import random
import re
import threading
import time

from locust import HttpUser, task, between, events

_RUN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = os.path.join(_RUN_DIR, "data", "testdata.csv")
_STATS_PATH = os.path.join(_RUN_DIR, "results", "apea_flow.json")

_APEA_BUILD = "2026-07-20 05:58:13 UTC"          # generation timestamp — confirms which script is running
# Memory applied to THIS build: facts earlier runs on this target taught APEA
# (e.g. region_policy, payment_method). Proves Run N+1 uses Run N's lesson with
# no LLM. Empty on a target's first run.
_APPLIED_MEMORY = {'platform': 'magento', 'last_success_build': '2026-07-19 16:09:02 UTC', 'payment_method': 'netterms', 'stock_policy': 'stop_when_out_of_stock'}
# Business Flow Model — the ordered checkout states this run must pass through,
# SOURCED FROM the Knowledge Base (patterns.yaml), not hard-coded here. The
# validator drives these states in order and fails fast on the first broken one.
_FLOW_MODEL = ['LOGIN', 'RESOLVE_PRODUCT', 'STOCK_CHECK', 'CART_CREATED', 'ITEMS_ADDED', 'ITEMS_VERIFIED', 'SHIPPING_AVAILABLE', 'SHIPPING_SELECTED', 'PAYMENT_AVAILABLE', 'PAYMENT_SELECTED', 'ORDER_CREATED']
_FLOW = {"login_ok": 0, "login_fail": 0, "orders": 0, "heals": 0, "captcha": 0,
         "order_ids": [], "build": _APEA_BUILD, "applied_memory": _APPLIED_MEMORY,
         "flow_model": _FLOW_MODEL}
_LOCK = threading.Lock()
_HAS_REST = True
_HAS_LOGIN_STEP = True
LOGIN_URLS = ['/uk/automation-and-control-systems/control-devices.html', '/customer/account/loginPost', '/customer/ajax/login']
_REST_PREFIX = '/uk/rest/uk/V1'
# Store view + GraphQL endpoint derived from the REST prefix. GraphQL is PUBLIC
# (no admin token) — unlike the admin-scoped /V1/products endpoint — so it is the
# right way to resolve a search term -> sku + stock with only a customer session.
_REST_PARTS = [p for p in (_REST_PREFIX or "").strip("/").split("/") if p]
_STORE = _REST_PARTS[0] if len(_REST_PARTS) >= 2 and _REST_PARTS[1] == "rest" else ""
_GRAPHQL_URL = ("/" + _STORE + "/graphql") if _STORE else "/graphql"
_CART_QTY = 1          # units added per add-to-cart (raises product load)
_REST_SKUS = ['c24071467', '124438669', '124436212', '124436213', '124436214', '124436215']        # SKUs harvested from the recording (empty-cart heal fallback)
_NEEDS_REST_CART = True   # flow does REST checkout but never records a REST cart-add
_FAITHFUL = False          # JMeter-style: replay recorded steps verbatim (no parameterize/correlate/heal)
# Abort the WHOLE run when checkout is definitively broken: after this many
# fail-fast checkout stops with zero orders, stop the Locust runner instead of
# hammering a broken checkout for the full duration. 0 disables (pure load mode).
_ABORT_AFTER = 3
# Checkout endpoints + payment policy — sourced from the Knowledge Base at
# generation time (single source of truth), not hardcoded here.
_EP = {'cart': '/carts/mine', 'items': '/carts/mine/items', 'estimate_shipping': '/carts/mine/estimate-shipping-methods-by-address-id', 'set_shipping': '/carts/mine/shipping-information', 'payment_methods': '/carts/mine/payment-methods', 'set_payment': '/carts/mine/set-payment-information', 'place_order': '/carts/mine/payment-information', 'agreements': '/carts/mine/checkout-agreements', 'agreements_fallback': '/checkoutAgreements'}
# Checkout-agreement (T&C) ids harvested from the recording (stable store config).
# The REST agreements endpoints are frequently not exposed (404), so these are the
# primary source; the REST fetch is only a fallback.
_AGREEMENT_IDS = ['2']
_ADD_CART_SIGNALS = ("cart/add", "carts/mine/items", "add-to-cart", "add_to_cart",
                     "cart/add.js", "cart/change.js", "add_item")
# CAPTCHA handling. A CAPTCHA cannot be solved by an HTTP load test by design;
# the correct approach is to DISABLE it in the test env, use the provider's test
# keys, or allowlist the load-generator IPs. When the env issues a bypass/test
# token, set it here and it is injected into login + checkout POST bodies.
_CAPTCHA_TOKEN = ''     # bypass / reCAPTCHA test-key response, or ""
_CAPTCHA_FIELD = ''     # extra field name to carry the token, or ""
# Fixes proposed by the optional AI self-repair pass (empty unless it ran).
_EXTRA_HEADERS = {}     # headers added to every checkout request
_FORCED_PAYMENT = ''   # payment method code to force at place-order
# Gateway test/sandbox-mode payment params. When the gateway is in test mode and
# a stored-card token / test profile is used, these are injected into the
# payment/place-order call so a real (test) card order completes under load —
# without a fresh single-use iframe token.
_PAYMENT_ADDL = {}       # dict merged into paymentMethod.additional_data
_PARAM_MAP = {'q': 'search_keyword', 'sku': 'product_id', 'qty': 'qty', 'countryId': 'country_id', 'regionId': 'region_id', 'region': 'region', 'regionCode': 'region_code', 'street': 'street', 'telephone': 'telephone', 'postcode': 'postcode', 'city': 'city', 'firstname': 'firstname', 'lastname': 'lastname', 'vatId': 'vat_id', 'shipping_carrier_code': 'shipping_carrier_code', 'shipping_method_code': 'shipping_method_code', 'method': 'payment_method', 'company': 'card_number', 'card_id': 'payment_token', 'purchaseordernumber': 'po_number'}             # {recorded field name: CSV column} generic parameterization
_CORRELATIONS = [{'name': 'form_key', 'extract': ['name="form_key"[^>]*value="([^"]+)"', '"form_key"\\s*:\\s*"([^"]+)"', 'form_key=([A-Za-z0-9]+)'], 'inject': ['form_key']}, {'name': 'uenc', 'extract': ['/uenc/([^/"\\\\]+)'], 'inject': ['uenc']}, {'name': 'quote_id', 'extract': ['"quote_id"\\s*:\\s*"?(\\d+)', '"entity_id"\\s*:\\s*"?(\\d+)'], 'inject': ['quoteId', 'quote_id', 'cartId', 'masked_id']}, {'name': 'order_id', 'extract': ['"order_id"\\s*:\\s*"?(\\d+)', '"increment_id"\\s*:\\s*"?([A-Za-z0-9]+)'], 'inject': ['order_id', 'orderId']}, {'name': 'address_id', 'extract': ['"addressId"\\s*:\\s*"?([A-Za-z0-9_\\-]+)', 'addressId=([A-Za-z0-9_\\-]+)', 'name="addressId"[^>]*value="([^"]+)"'], 'inject': ['addressId']}]       # JMeter-style extractor rules (capture from response, inject into request)


def _set_fields(body, fieldmap):
    """Overwrite body fields whose name is a key in `fieldmap` with that value.
    Works on dict, JSON-string and form-encoded bodies. Returns the same type it
    received; never mutates the input."""
    if not fieldmap:
        return body

    def _set(obj):
        if isinstance(obj, dict):
            for k in list(obj):
                if k in fieldmap and not isinstance(obj[k], (dict, list)) \
                        and str(fieldmap[k]) != "":
                    obj[k] = fieldmap[k]
                _set(obj[k])
        elif isinstance(obj, list):
            for it in obj:
                _set(it)

    if isinstance(body, str):
        b = body.strip()
        if b.startswith("{"):
            try:
                obj = json.loads(b)
                _set(obj)
                return json.dumps(obj)
            except Exception:
                return body
        from urllib.parse import parse_qsl, urlencode
        return urlencode([(k, fieldmap.get(k, v))
                          for k, v in parse_qsl(body, keep_blank_values=True)])
    if isinstance(body, dict):
        body = json.loads(json.dumps(body))     # deep copy so we don't mutate FLOW_STEPS
        _set(body)
    return body


def _apply_row(body, row):
    """Parameterize: overwrite recorded fields with THIS user's CSV row values via
    _PARAM_MAP (recorded field name -> CSV column). Any platform, any recording."""
    if not (_PARAM_MAP and row):
        return body
    fieldmap = {ff: row[col] for ff, col in _PARAM_MAP.items()
                if str(row.get(col, "")).strip() != ""}
    return _set_fields(body, fieldmap)


def _inject_payment(body):
    """Force the payment method + merge test-mode additional_data into a payment
    or place-order body (dict or JSON string). Returns the same type it received."""
    is_str = isinstance(body, str)
    data = body
    if is_str:
        if not body.strip():
            return body
        try:
            data = json.loads(body)
        except Exception:
            return body
    if not isinstance(data, dict):
        return body
    pm = data.get("paymentMethod")
    if not isinstance(pm, dict):
        pm = {}
        data["paymentMethod"] = pm
    if _FORCED_PAYMENT:
        pm["method"] = _FORCED_PAYMENT
    if _PAYMENT_ADDL:
        ad = pm.get("additional_data")
        if not isinstance(ad, dict):
            ad = {}
        ad.update(_PAYMENT_ADDL)
        pm["additional_data"] = ad
    return json.dumps(data) if is_str else data
# Common CAPTCHA response field names across platforms.
_CAPTCHA_FIELDS = ("g-recaptcha-response", "g_recaptcha_response", "h-captcha-response",
                   "recaptcha_response", "recaptcha", "captcha", "cf-turnstile-response",
                   "token")
# Markers that reveal a CAPTCHA challenge in a response body.
_CAPTCHA_MARKERS = ("g-recaptcha", "grecaptcha", "recaptcha/api", "www.google.com/recaptcha",
                    "h-captcha", "hcaptcha.com", "cf-turnstile", "challenges.cloudflare.com",
                    "please verify you are human", "invalid captcha", "captcha is required",
                    "captcha validation failed", "recaptcha validation failed")


def _has_captcha(txt):
    low = (txt or "").lower()
    return any(m in low for m in _CAPTCHA_MARKERS)


def _apply_captcha(body):
    """Inject the configured bypass/test token into a login/checkout body so a
    test-keyed or bypassed CAPTCHA env accepts the request. No-op when unset."""
    if not _CAPTCHA_TOKEN:
        return body
    if isinstance(body, dict):
        body = dict(body)
        placed = False
        for k in list(body):
            if k.lower() in _CAPTCHA_FIELDS:
                body[k] = _CAPTCHA_TOKEN
                placed = True
        if _CAPTCHA_FIELD:
            body[_CAPTCHA_FIELD] = _CAPTCHA_TOKEN
            placed = True
        if not placed:
            body["g-recaptcha-response"] = _CAPTCHA_TOKEN
    return body


def _write_stats():
    try:
        os.makedirs(os.path.dirname(_STATS_PATH), exist_ok=True)
        with open(_STATS_PATH, "w", encoding="utf-8") as fh:
            json.dump(_FLOW, fh)
    except Exception:
        pass


def _bump(key, n=1):
    with _LOCK:
        _FLOW[key] = _FLOW.get(key, 0) + n
        _write_stats()


# Assign a DISTINCT account to each virtual user (round-robin). This avoids
# concurrent users sharing one server-side cart/quote, which causes "no active
# cart" / "cart is locked" races and inconsistent order creation.
_USER_IDX = [0]
_ROW_IDX = [0]


def _next_cred(creds):
    if not creds:
        return {}
    with _LOCK:
        i = _USER_IDX[0]
        _USER_IDX[0] += 1
    return creds[i % len(creds)]


def _load_rows():
    """Every column of the testdata CSV as dict rows (for generic parameterization)."""
    rows = []
    try:
        with open(_DATA, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows.append({k: (v or "") for k, v in r.items() if k})
    except FileNotFoundError:
        pass
    return rows


def _next_row(rows):
    if not rows:
        return {}
    with _LOCK:
        i = _ROW_IDX[0]
        _ROW_IDX[0] += 1
    return rows[i % len(rows)]


def _record_order(order_id):
    """Count ONE confirmed order and remember its id/number for the report."""
    with _LOCK:
        _FLOW["orders"] = _FLOW.get("orders", 0) + 1
        if order_id and str(order_id) != "confirmed":
            _FLOW.setdefault("order_ids", []).append(str(order_id))
        _write_stats()


# ---- checkout timeline instrumentation -----------------------------------
_CHECKOUT_LOG = []          # step-by-step record of the most recent checkout


def _clog_reset():
    with _LOCK:
        _CHECKOUT_LOG[:] = []


def _mask(obj):
    """Truncated request payload for logging, with sensitive values masked."""
    if obj is None:
        return ""
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj)
    except Exception:
        return str(obj)[:400]
    s = re.sub(r'("(?:password|cvv|cvn|card_?number|cc_?number|securitycode|'
              r'payment_token|token|authorization)"\s*:\s*")[^"]*',
              lambda m: m.group(1) + "***", s, flags=re.I)
    return s[:400]


def _clog(step, method, url, status, ms, body, ok, extra="", req=None):
    """Record one checkout step (url, request payload, status, elapsed ms,
    truncated response body) and surface the timeline live via apea_flow.json."""
    entry = {"step": step, "method": method, "url": str(url), "status": status,
             "ms": round(float(ms), 1), "ok": bool(ok),
             "req": req or "", "body": (body or "")[:300], "extra": extra}
    with _LOCK:
        _CHECKOUT_LOG.append(entry)
        _FLOW["timeline"] = list(_CHECKOUT_LOG)
        _write_stats()


def _set_state(**kw):
    """Update the live Checkout State Report (cart id, item count, methods, state)."""
    with _LOCK:
        st = _FLOW.setdefault("checkout_state", {})
        st.update(kw)
        _write_stats()


def _clog_annotate(extra):
    """Attach an extra note (e.g. item count) to the most recent timeline entry."""
    with _LOCK:
        if _CHECKOUT_LOG:
            _CHECKOUT_LOG[-1]["extra"] = extra
            _FLOW["timeline"] = list(_CHECKOUT_LOG)
            _write_stats()


def _note_mode(m):
    """Record which checkout mode actually ran (faithful / rest-checkout /
    recorded-replay), so it's never ambiguous which path produced the results."""
    with _LOCK:
        _FLOW["mode"] = m
        _write_stats()


# ---- quote tracing --------------------------------------------------------
# Follow ONE quote/cart id through the whole checkout. If the id the token
# reports as its active cart differs from the id add-to-cart wrote to (or the id
# GET items reads back), the storefront session-quote and the token-quote have
# diverged — the classic cause of "empty cart" at shipping. This trace makes
# that divergence visible instead of leaving it to be inferred.
_QUOTE_TRACE = []

# Resolved-SKU cache: the CSV value is often a SEARCH TERM (manufacturer part
# number / name), NOT a sellable SKU. We search the catalog once per distinct
# term, resolve the real purchasable SKU, and cache it so we don't re-search on
# every iteration under load.
_SKU_CACHE = {}


def _qtrace_reset():
    with _LOCK:
        _QUOTE_TRACE[:] = []


def _qtrace(stage, quote_id, note=""):
    with _LOCK:
        _QUOTE_TRACE.append({"stage": stage, "quote_id": str(quote_id or ""), "note": note})
        _FLOW.setdefault("checkout_state", {})["quote_trace"] = list(_QUOTE_TRACE)
        _write_stats()


def _extract_quote_id(body):
    """Pull a Magento quote/cart id from a carts/mine response. Handles a bare
    numeric id (POST /carts/mine), a quote object with 'id', or a cart item with
    'quote_id'. Returns a string id or ''."""
    if body is None:
        return ""
    s = str(body).strip().strip('"')
    if re.fullmatch(r"\d+", s):
        return s
    try:
        d = json.loads(body)
    except Exception:
        return ""
    if isinstance(d, list) and d:
        d = d[0]
    if isinstance(d, dict):
        for k in ("quote_id", "id", "cart_id", "entity_id"):
            v = d.get(k)
            if v not in (None, "", 0):
                return str(v)
    return ""


def _clean_token(txt):
    """Return a valid Magento customer bearer token, or None.

    Magento's integration/customer/token returns the token as a BARE JSON string.
    Depending on config this is EITHER a ~32-char opaque token OR a JWT
    (header.payload.signature, base64url with '.', '_' and '-', well over 64
    chars). Accept both; reject only an HTML page or JSON error body (using those
    as a bearer causes 401 'consumer isn't authorized' on every carts/mine/*)."""
    t = (txt or "").strip().strip('"').strip()
    if not t or t[:1] in "<{[":              # HTML page / JSON error, not a token
        return None
    # opaque token or JWT: base64url alphabet plus dots, length >= 20
    if re.fullmatch(r"[A-Za-z0-9._-]{20,4096}", t):
        return t
    return None


def _extract_order_id(txt):
    """Pull a real order/quote id from a place-order or quote-submit response.

    Handles B2C orders AND B2B quote submission. A quote id is returned with a
    'Q' prefix so it is distinguishable from an order in the report. else None."""
    t = (txt or "").strip().strip('"')
    if t.isdigit() and t != "0":
        return t
    for pat in (r'"increment_id"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"order_number"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"orderNumber"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"order_id"\s*:\s*"?(\d+)',
                r'"entity_id"\s*:\s*"?(\d+)'):
        m = re.search(pat, t)
        if m:
            return m.group(1)
    # B2B negotiable-quote / RFQ submission returns a quote id / number.
    for pat in (r'"quote_id"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"quoteId"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"quote_number"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"rfq_number"\s*:\s*"?([A-Za-z0-9_-]+)'):
        m = re.search(pat, t)
        if m:
            return "Q" + m.group(1)
    return None


# Strong, platform-agnostic order/quote success URL signals (B2C + B2B).
_ORDER_URL_SIGNALS = ("onepage/success", "checkout/success", "checkout/onepage/success",
                      "order-received", "thank_you", "thankyou", "checkout/thank",
                      "order-confirmation", "/thank-you",
                      "quote/success", "quote-submitted", "negotiable_quote/quote/view",
                      "rfq/success")
# Order/quote-placement request fragments across platforms (B2C orders with/without
# payment, and B2B quote submission with/without payment).
_ORDER_PLACE_PATTERNS = ("payment-information", "placeorder", "place-order", "saveorder",
                         "checkout/onepage/save", "purchaseorder/save",
                         "wc-ajax=checkout", "/checkout.json", "submitorder",
                         "complete-order", "createorder",
                         # B2B quote submission
                         "negotiable-quote", "negotiablequote", "negotiable_quote",
                         "requestforquote", "request-for-quote", "request-quote",
                         "quote/save", "quotes/mine", "submitquote", "/rfq")
# Payment policy sourced from the Knowledge Base (single source of truth).
# Offline = no card token from a hosted iframe (B2B "order without payment" too);
# hosted = tokenizes in a 3rd-party iframe and cannot be HTTP-replayed.
_OFFLINE_PAYMENTS = ('purchaseorder', 'checkmo', 'banktransfer', 'cashondelivery', 'free', 'payorder', 'companycredit', 'check')
_HOSTED_GATEWAYS = ('cybersource', 'paradoxlabs', 'adyen', 'stripe', 'braintree', 'authorizenet', 'payflow', 'worldpay', 'sagepay', 'klarna', 'paypal', 'amazon')


def _confirm_order(url, status, txt):
    """Return an order id (or 'confirmed') if the response GENUINELY confirms an
    order, else None. Requires a real order id or a strong success URL/phrase —
    keyword-only matches are not counted, to avoid false positives."""
    if status is not None and status >= 400:
        return None
    low = (txt or "").lower()
    if '"error"' in low or "exception" in low or '"errors":true' in low:
        return None
    oid = _extract_order_id(txt)
    if oid:
        return oid
    u = (url or "").lower()
    if any(s in u for s in _ORDER_URL_SIGNALS):
        return "confirmed"
    if "thank you for your order" in low or "your order number" in low:
        return "confirmed"
    # B2B quote submission confirmations (order-without-payment path).
    if ("quote has been submitted" in low or "quote request" in low
            or "your quote" in low or "quote submitted" in low):
        return "confirmed"
    return None


def _looks_like_order(path, status, txt):
    """Return an order id/'confirmed' if this step confirms an order, else None."""
    oid = _confirm_order(path, status, txt)
    if oid:
        return oid
    p = (path or "").lower()
    if status < 400 and any(k in p for k in _ORDER_PLACE_PATTERNS):
        return _extract_order_id(txt)   # only count when a real order id comes back
    return None


def _load_testdata():
    creds, keywords, products = [], [], []
    try:
        with open(_DATA, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("username"):
                    creds.append({"username": row["username"].strip(),
                                  "password": (row.get("password") or "").strip()})
                if row.get("search_keyword"):
                    keywords.append(row["search_keyword"].strip())
                if row.get("product_id"):
                    products.append(row["product_id"].strip())
    except FileNotFoundError:
        pass
    return creds, keywords, products


FLOW_STEPS = [{'method': 'GET', 'path': '/uk/', 'name': 'GET uk', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/pdpdataprovider/index/index/', 'name': 'POST index/index', 'body': {'form_key': 'N9fkyY9c3CIfyvlb', 'recently_view_ids': '[]', 'sections': '["recently_view"]'}, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/websiteselector/index/renderList', 'name': 'GET index/renderList', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/customer/section/load/', 'name': 'GET section/load', 'body': {'sections': ''}, 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': False}, {'method': 'POST', 'path': '/uk/setcurrency/checkout/setsession', 'name': 'POST checkout/setsession', 'body': '{"currentUrl":"https://mcstaging.radwell.eu/uk/"}', 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': False}, {'method': 'GET', 'path': '/uk/customer/account/index/', 'name': 'GET account/index', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/customer/account/loginPost/referer/aHR0cHM6Ly9tY3N0YWdpbmcucmFkd2VsbC5ldS91ay9jdXN0b21lci9hY2NvdW50L2luZGV4Lw~~/', 'name': 'Login', 'body': {'form_key': 'N9fkyY9c3CIfyvlb', 'login[password]': 'Test@123', 'login[username]': 'uk0611@yopmail.com'}, 'asserts': [], 'login': True, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/catalogsearch/result/', 'name': 'GET catalogsearch/result', 'body': {'q': '0015E-2BAH-0012'}, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/buy/reuland-0015e-2bah-0012/24071467.html', 'name': 'GET reuland-0015e-2bah-0012/24071467ht', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/getitby/index/getitbyproductdetail', 'name': 'POST index/getitbyproductdetail', 'body': '{"form_key":"ZPX1sakGa7fwvdco","sku":"c24071467","childSku":[{"sku":"124438669","backorder":0,"rush_order_status":false},{"sku":"124436212","backorder":0,"rush_order_status":false},{"sku":"124436213","backorder":0,"rush_order_status":false},{"sku":"124436214","backorder":0,"rush_order_status":false},{"sku":"124436215","backorder":0,"rush_order_status":true}]}', 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': False}, {'method': 'POST', 'path': '/uk/checkout/cart/add/uenc/aHR0cHM6Ly9tY3N0YWdpbmcucmFkd2VsbC5ldS91ay9idXkvcmV1bGFuZC0wMDE1ZS0yYmFoLTAwMTIvMjQwNzE0NjcuaHRtbA~~/product/69388720/', 'name': 'POST product/69388720', 'body': {'extended_warranty': '0', 'form_key': 'ZPX1sakGa7fwvdco', 'item': '69388720', 'product': '69388720', 'qty': '1', 'related_product': '', 'selected_configurable_option': '', 'super_attribute[553]': '255529', 'uenc': 'aHR0cHM6Ly9tY3N0YWdpbmcucmFkd2VsbC5ldS91ay9idXkvcmV1bGFuZC0wMDE1ZS0yYmFoLTAwMTIvMjQwNzE0NjcuaHRtbA,,'}, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/checkout/cart/', 'name': 'GET checkout/cart', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/rest/uk/V1/carts/mine/estimate-shipping-methods?form_key=ZPX1sakGa7fwvdco', 'name': 'POST mine/estimate-shipping-methods', 'body': '{"address":{"countryId":"GB","regionId":null,"region":null}}', 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': True}, {'method': 'POST', 'path': '/uk/rest/uk/V1/carts/mine/totals-information?form_key=ZPX1sakGa7fwvdco', 'name': 'POST mine/totals-information', 'body': '{"addressInformation":{"shipping_carrier_code":"radwellshippingrate","shipping_method_code":"9004","address":{"countryId":"GB","regionId":null,"region":null}}}', 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': True}, {'method': 'POST', 'path': '/uk/getitby/index/getitbycart/', 'name': 'POST index/getitbycart', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': False}, {'method': 'GET', 'path': '/uk/checkout/', 'name': 'GET uk/checkout', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/en_GB/js-translation.json', 'name': 'GET en_GB/js-translationjson', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/base/Magento/base/default/Magento_Ui/templates/modal/modal-popup.html', 'name': 'GET modal/modal-popuphtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/base/Magento/base/default/Magento_Ui/templates/modal/modal-slide.html', 'name': 'GET modal/modal-slidehtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/base/Magento/base/default/Magento_Ui/templates/modal/modal-custom.html', 'name': 'GET modal/modal-customhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/base/Magento/base/default/Magento_Ui/templates/tooltip/tooltip.html', 'name': 'GET tooltip/tooltiphtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/base/Magento/base/default/Magento_Ui/templates/block-loader.html', 'name': 'GET templates/block-loaderhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/onepage.html', 'name': 'GET template/onepagehtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/banner/ajax/load/', 'name': 'GET ajax/load', 'body': {'requesting_page_url': 'https://mcstaging.radwell.eu/uk/checkout/', 'sections': '', '_': '1783579850991'}, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Ui/template/messages.html', 'name': 'GET template/messageshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/rest/uk/V1/carts/mine/estimate-shipping-methods-by-address-id', 'name': 'POST mine/estimate-shipping-methods-by-', 'body': '{"addressId":"7985"}', 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': True}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/payment.html', 'name': 'GET template/paymenthtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_NegotiableQuote/template/shipping.html', 'name': 'GET template/shippinghtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/sidebar.html', 'name': 'GET template/sidebarhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/authentication.html', 'name': 'GET template/authenticationhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Captcha/template/checkout/captcha.html', 'name': 'GET checkout/captchahtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/McFadyen_CheckoutOrderInstructions/template/payment-step-order-instructions-fields.html', 'name': 'GET template/payment-step-order-instru', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/payment-methods/list.html', 'name': 'GET payment-methods/listhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_CustomerBalance/template/payment/customer-balance.html', 'name': 'GET payment/customer-balancehtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Reward/template/payment/reward.html', 'name': 'GET payment/rewardhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/McFadyen_CustomerAddress/template/form/element/email.html', 'name': 'GET element/emailhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/shipping-address/form.html', 'name': 'GET shipping-address/formhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/summary.html', 'name': 'GET template/summaryhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/estimate-get-it-by-date.html', 'name': 'GET template/estimate-get-it-by-dateht', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/progress-bar.html', 'name': 'GET template/progress-barhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_NegotiableQuote/template/shipping-address/list.html', 'name': 'GET shipping-address/listhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_PaymentServicesPaypal/template/shipping-information.html', 'name': 'GET template/shipping-informationhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/McFadyen_CheckoutCustomization/template/form/element/customer_details.html', 'name': 'GET element/customer_detailshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_PaymentServicesPaypal/template/form/element/change-address.html', 'name': 'GET element/change-addresshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Ui/templates/form/field.html', 'name': 'GET form/fieldhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Ui/templates/group/group.html', 'name': 'GET group/grouphtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/summary/totals.html', 'name': 'GET summary/totalshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Ui/templates/form/element/input.html', 'name': 'GET element/inputhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Ui/templates/form/element/helper/tooltip.html', 'name': 'GET helper/tooltiphtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Ui/templates/form/element/select.html', 'name': 'GET element/selecthtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Tax/template/checkout/summary/subtotal.html', 'name': 'GET summary/subtotalhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_SalesRule/template/summary/discount.html', 'name': 'GET summary/discounthtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_GiftCardAccount/template/summary/gift-card-account.html', 'name': 'GET summary/gift-card-accounthtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_GiftWrapping/template/summary/totals.html', 'name': 'GET summary/totalshtml #2', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Reward/template/summary/reward.html', 'name': 'GET summary/rewardhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Tax/template/checkout/summary/shipping.html', 'name': 'GET summary/shippinghtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Weee/template/checkout/summary/weee.html', 'name': 'GET summary/weeehtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Tax/template/checkout/summary/tax.html', 'name': 'GET summary/taxhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_AdminUiSdkCustomFees/template/checkout/cart/totals/custom-fees.html', 'name': 'GET totals/custom-feeshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_CustomerBalance/template/summary/customer-balance.html', 'name': 'GET summary/customer-balancehtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Tax/template/checkout/summary/grand-total.html', 'name': 'GET summary/grand-totalhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_CompanyCredit/template/checkout/summary/grand-total.html', 'name': 'GET summary/grand-totalhtml #2', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_PurchaseOrder/template/checkout/shipping-address/address-renderer/default.html', 'name': 'GET address-renderer/defaulthtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/rest/uk/V1/carts/mine/shipping-information', 'name': 'POST mine/shipping-information', 'body': '{"addressInformation":{"shipping_address":{"customerAddressId":"7985","countryId":"GB","regionCode":"Staffordshire","region":"Staffordshire","customerId":"7889","street":["Lymedale Business Park"],"company":"MCf shipping","telephone":"15975325","fax":null,"postcode":"ST5 9QZ","city":"Newcastle","firstname":"Suganya","lastname":"Bala","middlename":null,"prefix":null,"suffix":null,"vatId":null,"customAttributes":[]},"billing_address":{"customerAddressId":"7985","countryId":"GB","regionCode":"Staffordshire","region":"Staffordshire","customerId":"7889","street":["Lymedale Business Park"],"company":"MCf shipping","telephone":"15975325","fax":null,"postcode":"ST5 9QZ","city":"Newcastle","firstname":"Suganya","lastname":"Bala","middlename":null,"prefix":null,"suffix":null,"vatId":null,"customAttributes":[],"saveInAddressBook":null},"shipping_method_code":"9004","shipping_carrier_code":"radwellshippingrate","extension_attributes":{}}}', 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': True}, {'method': 'GET', 'path': '/uk/rest/uk/V1/carts/mine/totals', 'name': 'GET mine/totals', 'body': {'_': '1783579850992'}, 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': True}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/shipping-address/shipping-method-list.html', 'name': 'GET shipping-address/shipping-method-l', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/summary/cart-items.html', 'name': 'GET summary/cart-itemshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/McFadyen_CheckoutOrderInstructions/template/shipping-step-order-instructions-fields.html', 'name': 'GET template/shipping-step-order-instr', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/shipping-address/shipping-method-item.html', 'name': 'GET shipping-address/shipping-method-i', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/summary/item/details.html', 'name': 'GET item/detailshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Tax/template/checkout/shipping_method/price.html', 'name': 'GET shipping_method/pricehtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/summary/item/details/thumbnail.html', 'name': 'GET details/thumbnailhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/summary/item/details/subtotal.html', 'name': 'GET details/subtotalhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/summary/item/details/message.html', 'name': 'GET details/messagehtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/rest/uk/V1/carts/mine/set-payment-information', 'name': 'POST mine/set-payment-information', 'body': '{"cartId":"180909","paymentMethod":{"method":"paradoxlabs_cybersource"}}', 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': True}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/ParadoxLabs_CyberSource/template/payment/secure-acceptance.html', 'name': 'GET payment/secure-acceptancehtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Paypal/template/payment/paypal-express-in-context.html', 'name': 'GET payment/paypal-express-in-contexth', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/McFadyen_NetTerms/template/payment/netterms.html', 'name': 'GET payment/nettermshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/McFadyen_WirePayment/template/payment/wirepayment.html', 'name': 'GET payment/wirepaymenthtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/pdl_cybs/secureAccept/getParams/', 'name': 'POST secureAccept/getParams', 'body': {'billing[city]': 'Newcastle', 'billing[company]': 'MCf shipping', 'billing[countryId]': 'GB', 'billing[firstname]': 'Suganya', 'billing[lastname]': 'Bala', 'billing[postcode]': 'ST5 9QZ', 'billing[regionCode]': 'Staffordshire', 'billing[region]': 'Staffordshire', 'billing[street][]': 'Lymedale Business Park', 'billing[telephone]': '15975325', 'card_id': '', 'form_key': 'ZPX1sakGa7fwvdco', 'guest_email': '', 'source': 'checkout'}, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/billing-address.html', 'name': 'GET template/billing-addresshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/payment/before-place-order.html', 'name': 'GET payment/before-place-orderhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_GiftCardAccount/template/payment/gift-card-information.html', 'name': 'GET payment/gift-card-informationhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_CheckoutAgreements/template/checkout/checkout-agreements.html', 'name': 'GET checkout/checkout-agreementshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_PurchaseOrder/template/checkout/billing-address/details.html', 'name': 'GET billing-address/detailshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/billing-address/list.html', 'name': 'GET billing-address/listhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/billing-address/form.html', 'name': 'GET billing-address/formhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/billing-address/actions.html', 'name': 'GET billing-address/actionshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_Checkout/template/contact-details.html', 'name': 'GET template/contact-detailshtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Magento/base/default/Magento_Checkout/template/shipping-information/list.html', 'name': 'GET shipping-information/listhtml', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/static/version1779429086/frontend/Mcfadyen/LumaCheckout/default/Magento_PurchaseOrder/template/checkout/shipping-information/address-renderer/default.html', 'name': 'GET address-renderer/defaulthtml #2', 'body': None, 'asserts': [], 'login': False, 'xhr': True, 'json': False, 'rest': False}, {'method': 'POST', 'path': '/uk/rest/uk/V1/carts/mine/payment-information', 'name': 'POST mine/payment-information', 'body': '{"cartId":"180909","billingAddress":{"customerAddressId":"7988","countryId":"US","regionId":"127","regionCode":"NY","region":"New York","customerId":"7889","street":["Route 99"],"company":"Billcom","telephone":"123456789","fax":null,"postcode":"11101","city":"Long Island","firstname":"Suganya","lastname":"Bala","middlename":null,"prefix":null,"suffix":null,"vatId":null,"customAttributes":[],"extension_attributes":{"purchaseordernumber":"","ordernotes":""}},"paymentMethod":{"method":"paradoxlabs_cybersource","additional_data":{"card_id":"10585c4eca7124c6364af67f94140c953037fbbf","cc_cid":"123","payerauth_session_id":"1_d31c2b25-7096-4f50-80b5-c13d8c166322","response_jwt":null,"save":false},"extension_attributes":{"agreement_ids":["2"]}}}', 'asserts': [], 'login': False, 'xhr': True, 'json': True, 'rest': True}, {'method': 'GET', 'path': '/uk/checkout/onepage/success/', 'name': 'GET onepage/success', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/websiteselector/index/currentWebsiteInformation', 'name': 'GET index/currentWebsiteInformation', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/', 'name': 'GET /', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/scopeinversion/scopes/current', 'name': 'GET /uk/scopeinversion/scopes/current', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/manufacturer/siemens', 'name': 'GET /uk/manufacturer/siemens', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/catalog/category/view/s/all-categories/id/20779/', 'name': 'GET /uk/catalog/category/view/s/all-categories/id/20779/', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/automation-and-control-systems.html', 'name': 'GET /uk/automation-and-control-systems.html', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}, {'method': 'GET', 'path': '/uk/automation-and-control-systems/control-devices.html', 'name': 'GET /uk/automation-and-control-systems/control-devices.html', 'body': None, 'asserts': [], 'login': False, 'xhr': False, 'json': False, 'rest': False}]


class WebsiteUser(HttpUser):
    """Replays the recorded checkout flow as one correlated, asserted session."""
    wait_time = between(2, 5)

    def on_start(self):
        self.credentials, self.search_keywords, self.product_ids = _load_testdata()
        self._rows = _load_rows()
        self._row = _next_row(self._rows)   # this user's full CSV row (all columns)
        self._vars = {}                     # correlation variables captured from responses
        self._form_key = None
        self._token = None
        self._address_id = None
        self._billing = None
        self._order_placed = False
        self._email = ""
        self._password = ""
        creds = _next_cred(self.credentials)   # distinct account per user (round-robin)
        self._email = creds.get("username", "")
        self._password = creds.get("password", "")
        if _FAITHFUL:
            return   # JMeter-style: no warm-up / token mint / login — replay verbatim
        with self.client.get("/", name="GET / (warmup)", catch_response=True) as r:
            self._form_key = (self.client.cookies.get("form_key")
                              or self._extract_form_key(r.text))
            self._capture(r.text)                        # seed correlation vars
            if self._form_key:
                self._vars["form_key"] = self._form_key
            if r.status_code < 400:
                r.success()
            else:
                r.failure("warmup status %s" % r.status_code)
        # If the recorded/discovered flow captured no login request, establish an
        # authenticated storefront session up front (best-effort Magento endpoints).
        if self._email and not _HAS_LOGIN_STEP:
            payload = {"username": self._email, "email": self._email,
                       "login[username]": self._email, "password": self._password,
                       "login[password]": self._password, "form_key": self._form_key or ""}
            payload = _apply_captcha(payload)
            logged_in = False
            for lp in LOGIN_URLS:
                with self.client.post(lp, data=payload,
                                      headers={"X-Requested-With": "XMLHttpRequest"},
                                      name="Login", catch_response=True) as r:
                    low = (r.text or "").lower()
                    captcha = _has_captcha(low) and not _CAPTCHA_TOKEN
                    ok = r.status_code < 400 and not captcha and not any(
                        s in low for s in ('"errors":true', "invalid login",
                                           "invalid email", "incorrect", "could not"))
                    if ok:
                        logged_in = True
                        r.success()
                    elif captcha:
                        _bump("captcha")
                        r.failure("[Login] CAPTCHA challenge detected — disable CAPTCHA on "
                                  "the test env, use provider test keys, or set a bypass token")
                    else:
                        r.failure("[Login] %s at %s" % (r.status_code, lp))
                if logged_in:
                    break
            _bump("login_ok" if logged_in else "login_fail")
        # Magento REST endpoints (carts/mine, checkout) need a customer bearer token
        if _HAS_REST and self._email:
            with self.client.post(_REST_PREFIX + "/integration/customer/token",
                                  json={"username": self._email, "password": self._password},
                                  name="REST customer token", catch_response=True) as r:
                tok = _clean_token(r.text)
                if r.status_code < 400 and tok:
                    self._token = tok
                    r.success()
                else:
                    # 200 with an HTML/garbage body means the token endpoint is not a
                    # clean API here; don't use a bad bearer (it causes 401 on mine/*).
                    r.failure("token status %s body=%s" % (r.status_code, (r.text or "")[:150]))
        # capture the logged-in customer's default shipping address id, so the
        # REST checkout calls use THIS user's address instead of the recorded one
        if self._token:
            with self.client.get(_REST_PREFIX + "/customers/me",
                                 headers={"Authorization": "Bearer %s" % self._token},
                                 name="REST customer profile", catch_response=True) as r:
                if r.status_code < 400:
                    try:
                        me = json.loads(r.text or "{}")
                        addrs = me.get("addresses") or []
                        dsid = me.get("default_shipping")
                        dbid = me.get("default_billing")
                        pick = next((a for a in addrs if str(a.get("id")) == str(dsid)), None) \
                            or (addrs[0] if addrs else None)
                        if pick:
                            self._address_id = pick.get("id")
                        bpick = next((a for a in addrs if str(a.get("id")) == str(dbid)), None) or pick
                        if bpick:
                            reg = bpick.get("region") or {}
                            self._billing = {
                                "firstname": bpick.get("firstname"),
                                "lastname": bpick.get("lastname"),
                                "street": bpick.get("street") or [],
                                "city": bpick.get("city"),
                                "country_id": bpick.get("country_id"),
                                "postcode": bpick.get("postcode"),
                                "telephone": bpick.get("telephone"),
                                "region": reg.get("region"),
                                "region_id": reg.get("region_id"),
                                "region_code": reg.get("region_code"),
                            }
                    except Exception:
                        pass
                    r.success()
                else:
                    r.failure("customer profile %s" % r.status_code)

    # recorded REST-checkout POSTs we replace with a clean API sequence
    _REST_CHECKOUT_STEPS = ("carts/mine/shipping-information", "estimate-shipping",
                            "carts/mine/totals-information", "set-payment-information",
                            "carts/mine/payment-information")

    @task(23)
    def browse_home(self):
        _u = '/'
        with self.client.get(_u, name='Home Page', catch_response=True) as r:
            r.success() if r.status_code < 400 else r.failure('Home Page ' + str(r.status_code))

    @task(23)
    def browse_search(self):
        terms = self.search_keywords or self.product_ids
        if not terms:
            return
        from urllib.parse import quote_plus as _qp
        _u = '/uk/catalogsearch/result/?q=' + _qp(str(random.choice(terms)))
        with self.client.get(_u, name='Search Results', catch_response=True) as r:
            r.success() if r.status_code < 400 else r.failure('Search Results ' + str(r.status_code))

    @task(23)
    def browse_pdp(self):
        _u = '/uk/automation-and-control-systems.html'
        with self.client.get(_u, name='Product Detail (PDP)', catch_response=True) as r:
            r.success() if r.status_code < 400 else r.failure('Product Detail (PDP) ' + str(r.status_code))

    @task(30)
    def checkout(self):
        self._order_placed = False
        # Faithful (JMeter-style): replay every recorded step verbatim, in order,
        # in one cookie-managed session — no parameterization, correlation, token
        # mint, or heal. Mirrors how JMeter runs the raw recording.
        if _FAITHFUL:
            _note_mode("faithful-replay")
            for step in FLOW_STEPS:
                self._run_raw(step)
            return
        # On a Magento REST target ALWAYS drive the order via the fail-fast
        # validator — never replay the recorded checkout POSTs (that produced the
        # estimate->totals->shipping->payment cascade of 400s). A missing token is
        # a clean STOP at Login, not a cascade.
        rest_order = _HAS_REST
        _note_mode("rest-checkout (validator)" if rest_order else "recorded-replay")
        for step in FLOW_STEPS:
            p = (step.get("path") or "").lower()
            # For a Magento REST target, SKIP the fragile recorded checkout POSTs —
            # we place the order via the robust API sequence below instead.
            if rest_order and any(k in p for k in self._REST_CHECKOUT_STEPS):
                continue
            self._run_step(step)
        if rest_order:
            self._rest_checkout()           # robust, API-driven order
        elif not self._order_placed:
            self._place_order()             # non-Magento: replay recorded order step

    def _run_raw(self, step):
        """Verbatim replay of one recorded step (JMeter-style): recorded body +
        recorded headers, cookies via the session, no mutation, no heal."""
        body = step.get("body")
        hdrs = dict(step.get("hdrs") or {})
        ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "").lower()
        req = {}
        if "application/json" in ctype or (isinstance(body, str) and body.strip()[:1] in "{["):
            if isinstance(body, dict):
                req["json"] = body
            else:
                hdrs.setdefault("Content-Type", "application/json")
                req["data"] = body
        else:
            req["data"] = body if isinstance(body, dict) else (body or None)
        with self.client.request(step["method"], step["path"], name=step["name"],
                                 headers=hdrs or None, catch_response=True, **req) as r:
            txt = r.text or ""
            ok = r.status_code < 400
            for a in step.get("asserts", []):
                if a and a not in txt:
                    ok = False
                    break
            oid = (_looks_like_order(step["path"], r.status_code, txt)
                   or _confirm_order(r.url, r.status_code, txt))
            if oid:
                _record_order(oid)
                self._order_placed = True
            if step.get("login"):
                _bump("login_ok" if ok else "login_fail")
            r.success() if ok else r.failure(
                "[%s] code=%s url=%s body=%s" % (step["name"], r.status_code, r.url, txt[:800]))

    def _rc(self, step, method, path, extra="", **kw):
        """One instrumented checkout API call: log url + status + elapsed ms +
        truncated body (+ optional note) to the timeline, mark success/failure.
        Returns (ok, status, body)."""
        name = kw.pop("name", step)
        reqp = kw.get("json") if "json" in kw else kw.get("data")
        t0 = time.time()
        with self.client.request(method, path, name=name, catch_response=True, **kw) as r:
            ms = (time.time() - t0) * 1000.0
            body = r.text or ""
            ok = r.status_code < 400
            _clog(step, method, r.url, r.status_code, ms, body, ok,
                  extra=extra, req=_mask(reqp))
            if ok:
                r.success()
            else:
                r.failure("[%s] code=%s body=%s" % (step, r.status_code, body[:300]))
            return ok, r.status_code, body

    @staticmethod
    def _reason(body):
        """Human-readable failure reason from a Magento error body."""
        try:
            j = json.loads(body or "")
            if isinstance(j, dict) and j.get("message"):
                msg = str(j["message"])
                for k, v in (j.get("parameters") or {}).items():
                    msg = msg.replace("%" + str(k), str(v))
                return msg
        except Exception:
            pass
        return (body or "")[:200] or "no response body"

    def _stop(self, step, status, reason):
        """Record a hard STOP at the first failed checkout step and halt this
        transaction. If checkout is definitively broken (repeated stops, no
        orders), abort the WHOLE run rather than hammer a broken checkout."""
        _set_state(stopped_at=step, stop_reason=reason, stop_status=status)
        _clog("STOP", "", "", status, 0, reason, False,
              extra="Stopped at '%s' (status %s): %s" % (step, status, reason))
        with _LOCK:
            _FLOW["checkout_stops"] = _FLOW.get("checkout_stops", 0) + 1
            n_stops = _FLOW["checkout_stops"]
            orders = _FLOW.get("orders", 0)
            _write_stats()
        # TERMINAL failures won't fix themselves by retrying — a re-run just repeats
        # the same error (and re-adds to cart, re-hitting "qty not available"). Abort
        # the WHOLE run on the FIRST such failure, not after _ABORT_AFTER tries.
        _low = (reason or "").lower()
        _terminal = any(s in _low for s in (
            "out of stock", "not purchasable", "requested qty is not available",
            "terms and conditions", "agreement", "payment method is not available",
            "only hosted-gateway", "no payment methods", "no usable payment",
            "no shipping methods", "could not find a product", "no purchasable",
            "no rest customer token", "no sku", "does not match any route",
            "quote mismatch"))
        _abort = orders == 0 and (_terminal or (_ABORT_AFTER and n_stops >= _ABORT_AFTER))
        if _abort:
            print("[APEA] Aborting run — %s: [%s] %s"
                  % ("terminal checkout failure" if _terminal
                     else "checkout broken (%d stops, 0 orders)" % n_stops, step, reason))
            _set_state(aborted=True, abort_reason="%s: %s" % (step, reason))
            try:
                self.environment.runner.quit()   # stop the whole Locust run
            except Exception:
                pass
        return False

    def _resolve_products(self, term):
        """Resolve an ORDERED LIST of candidate products (in-stock first) for a
        data-driven value via Magento GraphQL. Returning several search results —
        not child variants of one product — lets checkout AUTO-SUBSTITUTE to the
        next in-stock product when the first is out of stock (hybrid heal), so the
        run keeps going without faking anything. Cached per term.
        Each candidate: {sku, type_id, in_stock, item_options}."""
        term = str(term or "").strip()
        if not term:
            return []
        with _LOCK:
            if term in _SKU_CACHE:
                return list(_SKU_CACHE[term])
        query = ("query($q:String!){products(search:$q,pageSize:10){items{"
                 "sku stock_status __typename "
                 "... on ConfigurableProduct{variants{product{sku stock_status}}}}}}")
        headers = {"Content-Type": "application/json"}
        if _STORE:
            headers["Store"] = _STORE
        cands = []
        try:
            with self.client.post(_GRAPHQL_URL, name="Resolve: graphql",
                                  json={"query": query, "variables": {"q": term}},
                                  headers=headers, catch_response=True) as r:
                ok = r.status_code < 400
                data = json.loads(r.text or "{}") if ok else {}
                items = (((data.get("data") or {}).get("products") or {}).get("items") or [])
                (r.success() if (ok and items)
                 else r.failure("graphql resolve status=%s items=%d" % (r.status_code, len(items))))
                for it in items:
                    sku = it.get("sku")
                    in_stock = (str(it.get("stock_status") or "").upper() == "IN_STOCK")
                    if it.get("__typename") == "ConfigurableProduct":
                        variants = it.get("variants") or []
                        chosen = next((v.get("product") for v in variants
                                       if str((v.get("product") or {}).get("stock_status") or "").upper()
                                       == "IN_STOCK"), None)
                        if chosen and chosen.get("sku"):
                            sku, in_stock = chosen.get("sku"), True
                        elif variants and (variants[0].get("product") or {}).get("sku"):
                            sku, in_stock = variants[0]["product"]["sku"], False
                    if sku:
                        cands.append({"sku": str(sku), "type_id": it.get("__typename"),
                                      "in_stock": in_stock, "item_options": None})
        except Exception:
            cands = []
        # in-stock first (so substitution starts with the most likely to succeed)
        cands.sort(key=lambda c: 0 if c.get("in_stock") else 1)
        with _LOCK:
            _SKU_CACHE[term] = list(cands)
        return cands

    def _rest_checkout(self):
        """Checkout VALIDATOR: run the Magento REST order as a gated, fail-fast
        sequence. Each step is logged (url/status/ms/body); on the FIRST failure it
        STOPS and reports the exact step + reason — no cascade. Verifies the cart
        actually contains an item before shipping."""
        _clog_reset()
        _qtrace_reset()
        _set_state(state="START", cart_id="", item_count=0, shipping_methods=[],
                   payment_methods=[], stopped_at="", stop_reason="", order_id="",
                   quote_mismatch=False)
        auth = {"Authorization": "Bearer %s" % self._token}
        _qtrace("Customer token", "", "acquired (len=%d)" % len(self._token or ""))

        # Login (validated in on_start): a customer token is required for carts/mine.
        _clog("Login", "", "", 200 if self._token else 0, 0, "", bool(self._token),
              extra=("customer token acquired" if self._token else "no token"))
        if not self._token:
            return self._stop("Login", 0, "no REST customer token — cannot drive carts/mine")

        # Resolve CANDIDATE products (in-stock first) from the data-driven value.
        # Substitution keeps the run going honestly: if a product is out of stock,
        # we try the NEXT in-stock search result — not a fake success.
        term = (str(random.choice(self.product_ids)) if self.product_ids
                else (str(random.choice(self.search_keywords)) if self.search_keywords else ""))
        cands = self._resolve_products(term) if term else []
        if not cands and _REST_SKUS:
            cands = [{"sku": str(s), "type_id": None, "in_stock": None, "item_options": None}
                     for s in _REST_SKUS]
        _clog("Resolve SKU", "GET", _REST_PREFIX + "/search", 200 if cands else 404, 0, "",
              bool(cands), extra="term='%s' -> %d candidate(s): %s"
              % (term, len(cands), [c["sku"] for c in cands[:8]]))
        if not cands:
            return self._stop("Resolve SKU", 404,
                              "could not find any product for '%s' — catalog search returned "
                              "nothing; check the value or the store's search config" % term)

        # 1) Cart created (once — candidate products are added to this same quote)
        ok, st, body = self._rc("Cart created", "POST", _REST_PREFIX + _EP["cart"], headers=auth)
        if not ok:
            return self._stop("Cart created", st, self._reason(body))
        cart_id = _extract_quote_id(body) or (body or "").strip().strip('"')  # token's active quote
        _qtrace("POST /carts/mine (token active quote)", cart_id)
        _set_state(cart_id=cart_id, state="CART_CREATED")
        # 2) Items added — try candidates until one is actually purchasable. An
        # out-of-stock / "qty not available" response substitutes the NEXT product
        # (hybrid heal); a non-stock error is a real failure and stops at once.
        sku, qid_add, added, last_rsn = "", "", False, ""
        for _ci, _cand in enumerate(cands[:8]):
            _csku = _cand["sku"]
            cart_item = {"sku": _csku, "qty": _CART_QTY}
            if _cand.get("item_options"):
                cart_item["product_option"] = {
                    "extension_attributes": {"configurable_item_options": _cand["item_options"]}}
            ok, st, body = self._rc("Items added", "POST", _REST_PREFIX + _EP["items"],
                                    headers=auth, json={"cartItem": cart_item},
                                    extra="sku=%s qty=%s cart_id=%s (candidate %d/%d)"
                                    % (_csku, _CART_QTY, cart_id, _ci + 1, len(cands)))
            if ok:
                sku, added = _csku, True
                qid_add = _extract_quote_id(body)
                _qtrace("POST /carts/mine/items", qid_add, "sku=%s qty=%s" % (_csku, _CART_QTY))
                break
            last_rsn = self._reason(body)
            if any(s in last_rsn.lower() for s in ("requested qty is not available",
                    "out of stock", "not available", "in stock", "salable")):
                _clog_annotate("sku=%s not purchasable (%s) — substituting next candidate"
                               % (_csku, last_rsn))
                continue                      # out of stock -> try the next product
            return self._stop("Items added", st,          # non-stock error is terminal
                              "add-to-cart FAILED for sku='%s': %s" % (_csku, last_rsn))
        if not added:
            return self._stop("Items added", st,
                              "no purchasable product for '%s' — all %d candidate(s) are out "
                              "of stock (last: %s)" % (term, len(cands), last_rsn))
        _set_state(state="ITEMS_ADDED")
        # 3) Cart contains items  (the critical verification — count must be > 0)
        ok, st, body = self._rc("Cart contains items", "GET",
                                _REST_PREFIX + _EP["items"], headers=auth)
        if not ok:
            return self._stop("Cart contains items", st, self._reason(body))
        try:
            items = json.loads(body or "[]")
        except Exception:
            items = []
        n_items = len(items) if isinstance(items, list) else 0
        qid_get = _extract_quote_id(body)      # quote the token READS back
        _qtrace("GET /carts/mine/items", qid_get, "items=%d" % n_items)
        # Quote drift = the smoking gun. Compare the ids we captured; ignore blanks.
        seen = [q for q in (cart_id, qid_add, qid_get) if q]
        mismatch = len(set(seen)) > 1
        _set_state(item_count=n_items, state="ITEMS_VERIFIED", quote_mismatch=mismatch)
        _clog_annotate("items in cart: %d (sku=%s) | quote create=%s add=%s get=%s%s"
                       % (n_items, sku, cart_id or "?", qid_add or "?", qid_get or "?",
                          "  ⚠ QUOTE MISMATCH" if mismatch else ""))
        if mismatch:
            return self._stop("Cart contains items", st,
                              "QUOTE MISMATCH — add-to-cart wrote to quote %s but the token's active "
                              "quote is %s (GET read %s). The storefront session-quote and the "
                              "token-quote have diverged; that is why the cart looks empty at "
                              "shipping. STOP before shipping/payment."
                              % (qid_add or "?", cart_id or "?", qid_get or "?"))
        if n_items < 1:
            return self._stop("Cart contains items", st,
                              "cart is EMPTY after add (0 items) — sku='%s' likely not found / "
                              "disabled / out-of-stock / wrong store view, or the add wrote to a "
                              "different quote (quote create=%s add=%s get=%s). STOP before "
                              "shipping/payment." % (sku, cart_id or "?", qid_add or "?", qid_get or "?"))
        # 4) Shipping methods available
        ok, st, body = self._rc("Shipping methods available", "POST",
            _REST_PREFIX + _EP["estimate_shipping"],
            headers=auth, json={"addressId": self._address_id})
        if not ok:
            return self._stop("Shipping methods available", st, self._reason(body))
        method_code = carrier_code = None
        try:
            ms = json.loads(body or "[]")
            pick = next((m for m in ms if m.get("available")), (ms[0] if ms else None))
            if pick:
                method_code, carrier_code = pick.get("method_code"), pick.get("carrier_code")
        except Exception:
            pass
        if not (method_code and carrier_code):
            return self._stop("Shipping methods available", st,
                              "no shipping methods returned for this cart/address")
        _set_state(shipping_methods=[str(method_code)], state="SHIPPING_AVAILABLE")
        # 5) Set shipping information
        addr = self._addr()
        ok, st, body = self._rc("Set shipping information", "POST",
            _REST_PREFIX + _EP["set_shipping"], headers=auth,
            json={"addressInformation": {"shipping_address": addr, "billing_address": addr,
                  "shipping_method_code": method_code, "shipping_carrier_code": carrier_code}})
        if not ok:
            return self._stop("Set shipping information", st, self._reason(body))
        _qtrace("Shipping set", cart_id, "method=%s/%s" % (carrier_code, method_code))
        _set_state(state="SHIPPING_SELECTED")
        # 6) Payment methods available
        ok, st, body = self._rc("Payment methods available", "GET",
            _REST_PREFIX + _EP["payment_methods"], headers=auth)
        if not ok:
            return self._stop("Payment methods available", st, self._reason(body))
        codes, method = [], None
        try:
            ms = json.loads(body or "[]")
            codes = [m.get("code") for m in ms if m.get("code")]
            for pref in _OFFLINE_PAYMENTS:
                method = next((c for c in codes if pref in c.lower()), None)
                if method:
                    break
            if not method and codes:
                method = next((c for c in codes
                               if not any(g in c.lower() for g in _HOSTED_GATEWAYS)), codes[0])
        except Exception:
            pass
        if not codes:
            return self._stop("Payment methods available", st,
                              "no payment methods available on the cart")
        _set_state(payment_methods=codes, state="PAYMENT_AVAILABLE")
        # MEMORY: if an earlier run on this target found a payment method that
        # actually placed an order, prefer it (no LLM). User override still wins.
        _mem_pay = _APPLIED_MEMORY.get("payment_method")
        if _mem_pay and _mem_pay in codes:
            method = _mem_pay
        method = _FORCED_PAYMENT or method
        if not method:
            return self._stop("Payment methods available", st,
                              "no usable payment method among %s" % codes)
        pm = {"method": method}
        if _PAYMENT_ADDL:
            pm["additional_data"] = dict(_PAYMENT_ADDL)
        # Checkout agreements (terms & conditions). The order is rejected unless
        # active agreement ids are accepted. RECORDING-FIRST: the ids are stable
        # store config captured in the recorded place-order body, and the REST
        # agreements endpoints are often not exposed (404) — so prefer the recorded
        # ids and only hit REST as a fallback when the recording has none.
        _agr = [int(a) if str(a).isdigit() else a for a in _AGREEMENT_IDS]
        if _agr:
            _clog("Checkout agreements", "", "", 200, 0, "", True,
                  extra="from recording: agreement_ids=%s" % _agr)
        else:
            for _ep in (_EP["agreements"], _EP["agreements_fallback"]):
                _ok_a, _st_a, _body_a = self._rc("Checkout agreements", "GET",
                                                 _REST_PREFIX + _ep, headers=auth)
                if _ok_a:
                    try:
                        for _a in (json.loads(_body_a or "[]") or []):
                            _aid = _a.get("agreement_id", _a.get("agreementId"))
                            if _aid is not None and _a.get("is_active", True):
                                _agr.append(int(_aid) if str(_aid).isdigit() else str(_aid))
                    except Exception:
                        pass
                if _agr:
                    break
        if _agr:
            pm["extension_attributes"] = {"agreement_ids": _agr}
            _clog_annotate("accepted agreement_ids=%s" % _agr)
        payload = {"paymentMethod": pm, "billingAddress": self._addr()}
        # 7) Set payment information
        ok, st, body = self._rc("Set payment information", "POST",
            _REST_PREFIX + _EP["set_payment"], headers=auth, json=payload)
        if not ok:
            return self._stop("Set payment information", st,
                              "%s (tried method=%s, available=%s)" % (self._reason(body), method, codes))
        _set_state(state="PAYMENT_SELECTED")
        # 8) Order created (place order)
        ok, st, body = self._rc("Order created", "POST",
            _REST_PREFIX + _EP["place_order"], headers=auth, json=payload)
        oid = _extract_order_id(body) if ok else None
        if not ok:
            return self._stop("Order created", st,
                              "%s (tried method=%s, available=%s)" % (self._reason(body), method, codes))
        if not oid:
            return self._stop("Order created", st, "no order id returned; body=" + body[:200])
        _record_order(oid)
        self._order_placed = True
        with _LOCK:                       # remember the method that actually worked
            _FLOW["winning_payment"] = method
            _write_stats()
        _qtrace("Order created", cart_id, "order_id=%s" % oid)
        _set_state(state="ORDER_CREATED", order_id=str(oid))
        _clog("Order ID", "", "", st, 0, "", True, extra="Order ID: %s" % oid)
        return True

    def _ensure_cart(self, force=False):
        """Proactively add an item to the REST (carts/mine) cart before checkout.
        The recorded STOREFRONT add-to-cart writes to the session quote, but the
        token-authenticated REST checkout reads a DIFFERENT quote — which is why
        shipping-information reports an empty cart. Only runs when the flow does
        REST checkout and did not itself record a REST cart-add (or when forced)."""
        if not (_HAS_REST and self._token and (_NEEDS_REST_CART or force)):
            return
        sku = (str(random.choice(self.product_ids)) if self.product_ids
               else (str(random.choice(_REST_SKUS)) if _REST_SKUS
                     else (str(random.choice(self.search_keywords)) if self.search_keywords else None)))
        if not sku:
            return
        with self.client.post(_REST_PREFIX + "/carts/mine/items",
                              json={"cartItem": {"sku": sku, "qty": _CART_QTY}},
                              headers={"Authorization": "Bearer %s" % self._token},
                              name="Ensure cart item (REST)", catch_response=True) as r:
            if r.status_code < 400:
                r.success()
            else:
                # not fatal — the recorded flow may still populate the cart
                r.failure("ensure-cart %s %s" % (r.status_code, (r.text or "")[:150]))

    def _place_order(self):
        """Try platform strategies until an order is confirmed (any platform)."""
        # Strategy 1 — Magento REST place-order (customers with a bearer token).
        if self._token and self._rest_place_order():
            return
        # Strategy 2 — replay whatever order-placement request the recording captured
        # (works for Shopify / WooCommerce / SFCC / custom, driven by the recording).
        self._replay_order_step()

    def _addr(self):
        """Magento address dict (camelCase keys) from the logged-in customer's
        saved address. regionId is included ONLY when it is a positive integer —
        UK / no-numeric-region countries return a code (e.g. a NUTS value), which
        Magento rejects as regionId ('int expected'); there we omit it and rely on
        region (name) + regionCode + countryId. None values are dropped."""
        b = self._billing or {}
        addr = {"customerAddressId": self._address_id, "countryId": b.get("country_id"),
                "region": b.get("region"), "regionCode": b.get("region_code"),
                "street": b.get("street") or [], "city": b.get("city"),
                "postcode": b.get("postcode"), "firstname": b.get("firstname"),
                "lastname": b.get("lastname"), "telephone": b.get("telephone")}
        try:
            rid = int(b.get("region_id"))
            if rid > 0:
                addr["regionId"] = rid          # valid numeric region id only
        except (TypeError, ValueError):
            pass                                # non-int (e.g. NUTS code) -> omit
        return {k: v for k, v in addr.items() if v is not None}

    def _rest_place_order(self):
        auth = {"Authorization": "Bearer %s" % self._token}
        method, codes = None, []
        with self.client.get(_REST_PREFIX + "/carts/mine/payment-methods",
                             headers=auth, name="REST payment-methods",
                             catch_response=True) as r:
            try:
                ms = json.loads(r.text or "[]")
                codes = [m.get("code") for m in ms if m.get("code")]
                # Prefer OFFLINE / no-card methods. Hosted card gateways (CyberSource,
                # Adyen, Stripe, Braintree, etc.) tokenize the card in a 3rd-party
                # iframe that a pure-HTTP load test cannot complete, so we place the
                # order with an offline method when one is enabled on the cart.
                for pref in _OFFLINE_PAYMENTS:
                    method = next((c for c in codes if pref in c.lower()), None)
                    if method:
                        break
                if not method and codes:
                    # avoid obvious hosted-gateway codes when picking a fallback
                    method = next((c for c in codes
                                   if not any(g in c.lower() for g in _HOSTED_GATEWAYS)),
                                  codes[0])
            except Exception:
                pass
            r.success() if r.status_code < 400 else r.failure("pay-methods %s" % r.status_code)
        pm = {"method": _FORCED_PAYMENT or method or ""}
        if _PAYMENT_ADDL:
            pm["additional_data"] = dict(_PAYMENT_ADDL)   # test-mode / stored-card params
        payload = {"paymentMethod": pm}
        if self._billing:
            payload["billingAddress"] = self._addr()      # camelCase — correct regionId
        with self.client.post(_REST_PREFIX + "/carts/mine/payment-information",
                             json=payload, headers=auth, name="Place Order (REST)",
                             catch_response=True) as r:
            body = (r.text or "").strip().strip('"')
            oid = _extract_order_id(body) if r.status_code < 400 else None
            if oid:
                _record_order(oid)
                self._order_placed = True
                r.success()
                return True
            # surface the tried method + what the store ACTUALLY offers, so the
            # user knows exactly which offline code to set.
            r.failure("[Place Order REST] method=%s code=%s available=%s body=%s"
                      % (pm["method"], r.status_code, codes, body[:600]))
        return False

    def _replay_order_step(self):
        for s in FLOW_STEPS:
            if not any(k in (s.get("path") or "").lower() for k in _ORDER_PLACE_PATTERNS):
                continue
            body = s.get("body")
            if isinstance(body, dict):
                body = dict(body)
                if self._form_key and "form_key" in body:
                    body["form_key"] = self._form_key
            if _FORCED_PAYMENT or _PAYMENT_ADDL:
                body = _inject_payment(body)      # test-mode / stored-card params
            if _PARAM_MAP and self._row:
                body = _apply_row(body, self._row)   # card/shipping/etc. from CSV
            _rpath = s["path"]
            _rpath, body = self._correlate(_rpath, body)   # inject captured vars
            headers = {}
            if s.get("xhr"):
                headers["X-Requested-With"] = "XMLHttpRequest"
            if s.get("rest") and self._token:
                headers["Authorization"] = "Bearer %s" % self._token
            if s.get("json") and isinstance(body, str) and body.strip():
                headers["Content-Type"] = "application/json"
                req = {"data": body}
            elif s.get("json"):
                req = {"json": body if isinstance(body, dict) else {}}
            else:
                req = {"data": body if isinstance(body, dict) else (body or None)}
            with self.client.request(s["method"], _rpath, name="Place Order (replay)",
                                     headers=headers or None, catch_response=True, **req) as r:
                self._capture(r.text or "")
                oid = _confirm_order(r.url, r.status_code, r.text or "")
                if oid:
                    _record_order(oid)
                    self._order_placed = True
                    r.success()
                    return True
                r.failure("[Place Order replay] %s code=%s" % (s.get("name"), r.status_code))
        return False

    def _capture(self, text):
        """JMeter-style extractor: pull correlation values out of a response into
        self._vars using the configured regexes (first match wins per variable)."""
        if not (_CORRELATIONS and text):
            return
        for c in _CORRELATIONS:
            for pat in c.get("extract", []):
                try:
                    m = re.search(pat, text)
                except Exception:
                    continue
                if m:
                    self._vars[c["name"]] = m.group(1)
                    break

    def _correlate(self, path, body):
        """Inject captured correlation variables into this request's URL params,
        known path slots (uenc) and body fields."""
        if not (_CORRELATIONS and self._vars):
            return path, body
        from urllib.parse import quote_plus as _qp
        fieldmap = {}
        for c in _CORRELATIONS:
            val = self._vars.get(c["name"])
            if val is None or val == "":
                continue
            for f in c.get("inject", []):
                path = re.sub(r"([?&]" + re.escape(f) + r"=)[^&]*",
                              lambda m, v=val: m.group(1) + _qp(str(v)), path)
                fieldmap[f] = val
            if c["name"] == "uenc":
                path = re.sub(r"(/uenc/)[^/]+", lambda m, v=val: m.group(1) + str(v), path)
        if fieldmap:
            body = _set_fields(body, fieldmap)
        return path, body

    def _run_step(self, step, _healing=False):
        path = step["path"]
        body = step.get("body")
        if isinstance(body, dict):
            body = dict(body)
            if self._form_key and "form_key" in body:
                body["form_key"] = self._form_key
            if step.get("login"):
                for k in list(body):
                    lk = k.lower()
                    if self._email and ("email" in lk or "user" in lk or lk == "login"):
                        body[k] = self._email
                    if self._password and "pass" in lk:
                        body[k] = self._password
                body = _apply_captcha(body)
            elif _CAPTCHA_TOKEN and any(k in (path or "").lower()
                                        for k in _ORDER_PLACE_PATTERNS):
                body = _apply_captcha(body)
        # search using the test data: a search_keyword OR a product_id/name/any id
        terms = self.search_keywords or self.product_ids
        if terms and ("catalogsearch" in path or "q=" in path):
            from urllib.parse import quote_plus as _qp
            kw = _qp(str(random.choice(terms)))
            path = re.sub(r"([?&]q=)[^&]*", lambda m: m.group(1) + kw, path)
        # add-to-cart using product ids from the uploaded CSV
        if self.product_ids and "checkout/cart/add" in path and "/product/" in path:
            pid = str(random.choice(self.product_ids))
            path = re.sub(r"(/product/)\d+", lambda m: m.group(1) + pid, path)
        # cart quantity override: raise units per add-to-cart to increase load
        _low = (path or "").lower()
        if _CART_QTY > 1 and any(k in _low for k in _ADD_CART_SIGNALS):
            path = re.sub(r"([?&](?:qty|quantity)=)\d+",
                          lambda m: m.group(1) + str(_CART_QTY), path)
            if isinstance(body, dict):
                for bk in list(body):
                    if bk.lower() in ("qty", "quantity"):
                        body[bk] = _CART_QTY
                ci = body.get("cartItem")
                if isinstance(ci, dict) and "qty" in ci:
                    ci["qty"] = _CART_QTY
            elif isinstance(body, str) and body:
                body = re.sub(r'("(?:qty|quantity)"\s*:\s*)\d+',
                              lambda m: m.group(1) + str(_CART_QTY), body)
                body = re.sub(r'((?:^|&)(?:qty|quantity)=)\d+',
                              lambda m: m.group(1) + str(_CART_QTY), body)
        # inject THIS customer's shipping address id into REST checkout bodies
        if self._address_id and isinstance(body, str) and body:
            aid = str(self._address_id)
            for fld in ("addressId", "customer_address_id", "customerAddressId"):
                body = re.sub(r'("' + fld + r'"\s*:\s*)"?\d+"?',
                              lambda m, a=aid: m.group(1) + a, body)
        # gateway test-mode payment: force method + merge additional_data on the
        # payment / place-order call so a real (test) card order completes
        if (_FORCED_PAYMENT or _PAYMENT_ADDL) and any(
                k in (path or "").lower()
                for k in ("payment-information", "set-payment", "placeorder", "place-order")):
            body = _inject_payment(body)
        # generic parameterization: card / shipping / billing / contact / etc.
        if _PARAM_MAP and self._row:
            body = _apply_row(body, self._row)
        # correlation: inject values captured from earlier responses (form_key,
        # tokens, ids, dynamic iframe URLs, …) into this request
        path, body = self._correlate(path, body)
        headers = {}
        if step.get("xhr"):
            headers["X-Requested-With"] = "XMLHttpRequest"
        if step.get("rest") and self._token:
            headers["Authorization"] = "Bearer %s" % self._token
        if _EXTRA_HEADERS:
            headers.update(_EXTRA_HEADERS)   # AI self-repair proposed headers
        # treat as JSON if the recording said so, OR it's a GraphQL endpoint, OR the
        # body clearly looks like JSON — otherwise servers reject with 400 content-type
        _json = (step.get("json")
                 or (path or "").split("?")[0].rstrip("/").endswith("/graphql")
                 or (isinstance(body, str) and body.strip()[:1] in "{["))
        req = {}
        if _json:
            if isinstance(body, dict):
                req["json"] = body
            elif isinstance(body, str) and body.strip():
                headers["Content-Type"] = "application/json"
                req["data"] = body
            else:
                req["json"] = {}
        else:
            req["data"] = body if isinstance(body, dict) else (body or None)
        path = re.sub(r"^/{2,}", "/", path)          # collapse accidental leading //
        with self.client.request(step["method"], path, name=step["name"],
                                 headers=headers or None, catch_response=True, **req) as r:
            txt = r.text or ""
            self._capture(txt)                # harvest correlation vars from response
            ok = r.status_code < 400
            for a in step.get("asserts", []):
                if a and a not in txt:
                    ok = False
                    break
            if step.get("login"):
                low = txt.lower()
                captcha = _has_captcha(low) and not _CAPTCHA_TOKEN
                bad = captcha or any(s in low for s in ('"errors":true', "invalid login",
                          "invalid email", "incorrect", "could not"))
                ok = ok and not bad
                _bump("login_ok" if ok else "login_fail")
                if captcha:
                    _bump("captcha")
                if ok:
                    r.success()
                elif captcha:
                    r.failure("[Login] CAPTCHA challenge detected — disable CAPTCHA on the "
                              "test env, use provider test keys, or set a bypass token")
                else:
                    r.failure("[Login] FAILED code=%s body=%s" % (r.status_code, txt[:800]))
                return
            _oid = (_looks_like_order(step["path"], r.status_code, txt)
                    or _confirm_order(r.url, r.status_code, txt))
            if _oid:
                _record_order(_oid)
                self._order_placed = True
            if ok:
                r.success()
                return
            if _has_captcha(txt) and not _CAPTCHA_TOKEN:
                _bump("captcha")
                r.failure("[%s] CAPTCHA challenge detected — disable CAPTCHA on the test "
                          "env, use provider test keys, or set a bypass token" % step["name"])
            else:
                r.failure("[%s] code=%s url=%s body=%s"
                          % (step["name"], r.status_code, r.url, txt[:800]))
            fail_status, fail_txt = r.status_code, txt
        # ---- auto-heal: recognize the failure, fix it in-run, and retry once ----
        if not _healing and self._heal(step, fail_status, fail_txt):
            _bump("heals")
            self._run_step(step, _healing=True)

    def _heal(self, step, status, txt):
        """Deterministic run-time self-healing for common checkout failures.

        Returns True if a remedy was applied so the caller retries the step once.
        Remedies are generic Magento REST / data-driven — nothing site-specific.
        """
        low = (txt or "").lower()
        # 1) REST checkout reports an empty cart -> add an item to the REST quote
        if self._token and ("empty cart" in low or "add an item to cart" in low
                            or "cart is empty" in low):
            sku = (random.choice(self.product_ids) if self.product_ids
                   else (random.choice(_REST_SKUS) if _REST_SKUS
                         else (random.choice(self.search_keywords) if self.search_keywords else None)))
            if not sku:
                return False
            with self.client.post(_REST_PREFIX + "/carts/mine/items",
                                  json={"cartItem": {"sku": str(sku), "qty": _CART_QTY}},
                                  headers={"Authorization": "Bearer %s" % self._token},
                                  name="AUTO-HEAL add cart item", catch_response=True) as hr:
                if hr.status_code < 400:
                    hr.success()
                    return True
                hr.failure("heal add-item %s body=%s" % (hr.status_code, (hr.text or "")[:120]))
            return False
        # 2) payment reports the shipping address missing -> run shipping-information first
        if "shipping address is missing" in low or "set the address" in low:
            for s in FLOW_STEPS:
                if "shipping-information" in (s.get("path") or ""):
                    self._run_step(dict(s), _healing=True)
                    return True
        # 3) token/consumer auth expired -> refresh the customer bearer token
        if status == 401 and self._email:
            with self.client.post(_REST_PREFIX + "/integration/customer/token",
                                  json={"username": self._email, "password": self._password},
                                  name="AUTO-HEAL refresh token", catch_response=True) as hr:
                tok = _clean_token(hr.text)
                if hr.status_code < 400 and tok:
                    self._token = tok
                    hr.success()
                    return True
                hr.failure("heal token %s" % hr.status_code)
        return False

    @staticmethod
    def _extract_form_key(html):
        m = re.search(r'name="form_key"[^>]*value="([^"]+)"', html or "")
        return m.group(1) if m else None


@events.quitting.add_listener
def _log_summary(environment, **kwargs):
    stats = environment.stats.total
    fr = stats.fail_ratio * 100 if stats.num_requests else 0
    print("\n[APEA] Requests=%s Failures=%s (%.2f%%)"
          % (stats.num_requests, stats.num_failures, fr))
    print("[APEA] Login success=%s failure=%s  Orders created=%s  Auto-heals=%s"
          % (_FLOW["login_ok"], _FLOW["login_fail"], _FLOW["orders"], _FLOW["heals"]))
    if _APPLIED_MEMORY:
        print("[APEA] Applied from memory (no LLM): %s" % _APPLIED_MEMORY)
    if _FLOW_MODEL:
        _reached = (_FLOW.get("checkout_state") or {}).get("state", "")
        print("[APEA] Business flow model (from KB): %s" % " -> ".join(_FLOW_MODEL))
        print("[APEA] Reached state: %s" % (_reached or "-"))
    if _FLOW.get("captcha"):
        print("[APEA] CAPTCHA challenges hit=%s — disable CAPTCHA on the test env, use "
              "provider test keys, or set a bypass token" % _FLOW["captcha"])
    _ids = _FLOW.get("order_ids") or []
    if _ids:
        print("[APEA] Order ids: %s" % ", ".join(_ids[:30]))
    _tl = _FLOW.get("timeline") or []
    if _tl:
        print("\n[APEA] Checkout timeline (last checkout):")
        for _e in _tl:
            _mark = "OK  " if _e.get("ok") else "FAIL"
            _extra = (" -> " + str(_e["extra"])) if _e.get("extra") else ""
            print("  [%s] %-26s %s  %sms%s"
                  % (_mark, _e.get("step"), _e.get("status"), _e.get("ms"), _extra))
    _cs = _FLOW.get("checkout_state") or {}
    if _cs:
        print("\n[APEA] Checkout State Report  (mode=%s, build=%s)"
              % (_FLOW.get("mode"), _FLOW.get("build")))
        print("  reached state : %s" % _cs.get("state"))
        print("  cart id       : %s" % _cs.get("cart_id"))
        print("  items in cart : %s" % _cs.get("item_count"))
        print("  shipping meths: %s" % _cs.get("shipping_methods"))
        print("  payment meths : %s" % _cs.get("payment_methods"))
        _qt = _cs.get("quote_trace") or []
        if _qt:
            print("  quote trace   :")
            for _q in _qt:
                _n = ("  (%s)" % _q["note"]) if _q.get("note") else ""
                print("      %-34s quote=%s%s" % (_q.get("stage"), _q.get("quote_id") or "-", _n))
            if _cs.get("quote_mismatch"):
                print("      >> QUOTE MISMATCH — add-to-cart and the token's active quote differ")
        if _cs.get("stopped_at"):
            print("  >> STOPPED at '%s' (%s): %s"
                  % (_cs.get("stopped_at"), _cs.get("stop_status"), _cs.get("stop_reason")))
        if _cs.get("order_id"):
            print("  >> ORDER ID   : %s" % _cs.get("order_id"))
    _write_stats()
