"""Payment Analyzer — payment as a FIRST-CLASS discovered dependency.

APEA used to treat a payment token like any other correlation ("some dynamic value,
capture and re-inject"). That breaks on modern checkouts, where the token is minted
by a provider SDK / hosted iframe and may not be reproducible over HTTP at all. This
module answers the questions that actually decide how to load-test a payment:

    1. Which payment PROVIDER is in play?            -> provider
    2. What INTEGRATION is it?                        -> integration_type
       (hosted_iframe / redirect / embedded_sdk / direct_api)
    3. Where is the TOKEN generated?                 -> token.endpoint / token.field
    4. Can the token be reproduced via API, or does
       it need a browser?                            -> replayable + strategy
    5. Which merchant field CONSUMES the token?      -> correlation.inject

…and emits a single structured **Payment Profile** the rest of APEA can act on.

It reuses agents/payment_replay.py for the provider + replayability verdict (which is
built from the captured payment network sequence + KB replay_profiles), and adds two
things payment_replay does not: the integration-type taxonomy, and a payment-focused
CORRELATION MINER that discovers, from the recorded flow, the token's source field
($.id) and the merchant request field that consumes it (payment_method_id).

Pure analysis: never drives a browser, never places an order, never stores a token
value (only field names / endpoints / JSON paths). Never raises; returns None when
there's no payment to profile.
"""
from __future__ import annotations

import re

# --- integration taxonomy ----------------------------------------------------
HOSTED_IFRAME = "hosted_iframe"     # card entered in a cross-origin gateway iframe
REDIRECT = "redirect"               # top-level redirect to the gateway (3DS/PayPal)
EMBEDDED_SDK = "embedded_sdk"       # provider JS tokenizes via XHR on the merchant page
DIRECT_API = "direct_api"           # token/card posted straight to a merchant API
UNKNOWN = "unknown"

# --- execution strategies ----------------------------------------------------
API_REPLAY = "api_replay"           # reproduce token-gen + correlate the token (HTTP)
BROWSER_ASSISTED = "browser_assisted"  # discover via browser, then API-replay if possible
HYBRID = "hybrid"                   # browser pool mints tokens, Locust consumes them
OFFLINE = "offline"                 # no card token at all (offline payment method)

# token-shaped values (Stripe pm_/tok_/pi_, long hex ids, JWTs) — used to PROVE a
# value flows from a token response into a later request. Never stored.
_TOKEN_VALUE_RE = re.compile(
    r'\b((?:pm|tok|pi|src|card|cus|ba|seti)_[A-Za-z0-9]{6,}|'
    r'[A-Fa-f0-9]{24,}|eyJ[A-Za-z0-9._-]{20,})\b')
# a field that HOLDS a token in a JSON body (source side, e.g. the token response).
_TOKEN_FIELD_RE = re.compile(
    r'"(id|token|nonce|transientToken|transient_token|paymentMethod|'
    r'payment_method|clientSecret|client_secret)"\s*:\s*"([^"]{6,})"', re.I)
# a field in the MERCHANT request that consumes the token (inject side).
_CONSUMER_FIELD_RE = re.compile(
    r'"(payment_method_id|paymentMethodId|payment_method|paymentToken|'
    r'payment_token|payment_method_nonce|paymentMethodNonce|cc_token|nonce|'
    r'source|token|transientToken)"\s*:\s*"?([^",}\s]+)', re.I)

_IFRAME_HOSTS = ("secureacceptance", "flex.cybersource", "js.stripe.com",
                 "elements-inner", "checkoutshopper", "adyen", "braintree",
                 "hostedpayment", "hpp", "checkout.com")
_REDIRECT_HINTS = ("acs", "3ds", "threeds", "cardinalcommerce", "paypal",
                   "return_url", "returnurl", "/redirect")
_SDK_HINTS = ("api.stripe.com/v1/payment_methods", "api.stripe.com/v1/tokens",
              "microform", "createtoken", "tokenize", "clienttoken")


def _s(v) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _flow_steps(discovery: dict, analysis: dict) -> list:
    """Ordered (path, request-text, response-text) across the recorded flow and the
    runtime timeline — whichever carries bodies. Best-effort."""
    out = []
    for s in (discovery.get("flow") or []):
        out.append({"path": _s(s.get("path") or s.get("url") or s.get("name")),
                    "req": _s(s.get("body") or s.get("req")),
                    "resp": _s(s.get("resp") or s.get("response"))})
    for s in ((analysis.get("flow") or {}).get("timeline") or []):
        out.append({"path": _s(s.get("url") or s.get("name")),
                    "req": _s(s.get("req")), "resp": _s(s.get("resp") or s.get("body"))})
    return out


def _token_correlation(discovery: dict, analysis: dict) -> dict | None:
    """Discover the token's SOURCE field and the merchant field that CONSUMES it —
    the payment correlation APEA should generate ($.id -> payment_method_id). Prefers
    a PROVEN linkage (same value seen in a token response then in a later request);
    falls back to detecting the consumer field by name on the order request."""
    steps = _flow_steps(discovery, analysis)

    # (1) token values produced by some step (in its response or body).
    produced = []
    for st in steps:
        blob = st["resp"] or st["req"]
        for m in _TOKEN_FIELD_RE.finditer(blob or ""):
            field, val = m.group(1), m.group(2)
            if _TOKEN_VALUE_RE.search(val) or len(val) >= 12:
                produced.append({"value": val, "field": field,
                                 "path": "$." + field, "source": st["path"][:120]})

    # (2) PROVEN correlation: a produced value reused in a LATER request body.
    for p in produced:
        for st in steps:
            body = st["req"] or ""
            if p["value"] and p["value"] in body and st["path"] and st["path"] != p["source"]:
                inject = None
                for m in _CONSUMER_FIELD_RE.finditer(body):
                    if p["value"] in m.group(0):
                        inject = m.group(1)
                        break
                if inject:
                    return {"extract": p["path"], "token_field": p["field"],
                            "token_endpoint": p["source"], "inject": inject,
                            "consumer_endpoint": st["path"][:120],
                            "required": True, "proven": True, "confidence": "high"}

    # (3) fallback: a token-consumer field by NAME on an order/place request.
    for st in steps:
        _pl = st["path"].lower()
        if any(k in _pl for k in ("order", "payment-information", "placeorder",
                                  "checkout", "/pay")):
            m = _CONSUMER_FIELD_RE.search(st["req"] or "")
            if m:
                return {"extract": None, "token_field": None, "token_endpoint": None,
                        "inject": m.group(1), "consumer_endpoint": st["path"][:120],
                        "required": True, "proven": False, "confidence": "medium"}
    return None


def _integration_type(captured: list, corr: dict | None) -> str:
    blob = " ".join((c.get("url") or "") for c in (captured or [])).lower()
    tok_ep = ((corr or {}).get("token_endpoint") or "").lower()
    if any(h in blob for h in _IFRAME_HOSTS):
        return REDIRECT if any(h in blob for h in _REDIRECT_HINTS) else HOSTED_IFRAME
    if any(h in blob for h in _SDK_HINTS) or any(h in tok_ep for h in _SDK_HINTS):
        return EMBEDDED_SDK
    if any(h in blob for h in _REDIRECT_HINTS):
        return REDIRECT
    if corr and corr.get("token_endpoint"):
        return DIRECT_API
    return UNKNOWN


def _strategy(replayable, corr: dict | None) -> str:
    has = bool(corr)
    if replayable in (True, "true") and has:
        return API_REPLAY
    if replayable == "conditional":
        return BROWSER_ASSISTED
    if replayable in (False, "false"):
        return HYBRID
    return API_REPLAY if has else BROWSER_ASSISTED


def _summary(provider, itype, strategy, corr, replayable) -> str:
    corr_txt = ("token %s from %s -> %s in %s" % (
        corr.get("extract") or corr.get("token_field") or "token",
        corr.get("token_endpoint") or "provider", corr.get("inject"),
        corr.get("consumer_endpoint"))) if corr else "no token correlation found"
    return ("%s (%s), replayable=%s -> strategy=%s; %s" % (
        provider, itype, replayable, strategy, corr_txt))


def analyze(discovery: dict | None, analysis: dict | None,
            detected_gateway: str = "") -> dict | None:
    """Build the Payment Profile, or None if there's no payment to profile."""
    try:
        from . import payment_replay
        discovery = discovery or {}
        analysis = analysis or {}
        bt = (analysis.get("browser_track") or {}).get("summary") or {}
        captured = bt.get("payment_network") or []

        cls = payment_replay.classify(bt, detected_gateway=detected_gateway) or {}
        provider = cls.get("gateway") or detected_gateway or "unknown"
        corr = _token_correlation(discovery, analysis)

        # nothing to say: no provider, no captured payment traffic, no token corr.
        if provider in ("", "unknown", None) and not captured and not corr:
            return None

        itype = _integration_type(captured, corr)
        replayable = cls.get("replayable", "unknown")
        strategy = _strategy(replayable, corr)
        return {
            "provider": provider,
            "integration_type": itype,
            "replayable": replayable,
            "strategy": strategy,
            "token": ({"endpoint": corr.get("token_endpoint"),
                       "field": corr.get("token_field")} if corr else None),
            "correlation": corr,
            "mint": cls.get("mint"),
            "confirm_endpoint_hint": cls.get("confirm_endpoint_hint"),
            "confidence": corr.get("confidence") if corr else cls.get("confidence", "low"),
            "reasons": cls.get("reasons", []),
            "blockers": cls.get("blockers", []),
            "summary": _summary(provider, itype, strategy, corr, replayable),
        }
    except Exception:
        return None
