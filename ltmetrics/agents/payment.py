"""Payment method detection & classification — the single source of truth.

Two jobs, both deterministic (no LLM):

1. classify_payment(code) -> "hosted" | "offline" | "unknown"
   Generic, KB-driven. A "hosted" method tokenizes the card in a 3rd-party
   iframe (Stripe, CyberSource, Adyen, ...) and can only be completed by the
   real-browser (Playwright) track. An "offline" method (netterms, purchaseorder,
   banktransfer, ...) places the order over plain HTTP with no card token.

2. list_payment_methods(...) -> the REAL methods available on the target cart,
   each tagged with its kind, so the UI can show them for selection BEFORE a
   script is generated. Prefers a LIVE probe (authenticated cart) and falls back
   to methods observed in an uploaded recording.

Nothing here is tied to a particular merchant: the classification reads the
`hosted_gateways` / `offline_payments` lists from the Knowledge Base
(platform_rules.yaml), so teaching LT Metrics a new method is a one-line KB edit.
"""
from __future__ import annotations

import json
import re

# Sane fallbacks used only if the KB is missing/unreadable, so classification
# never silently returns "unknown" for a well-known method.
_HOSTED_FALLBACK = ["cybersource", "paradoxlabs", "adyen", "stripe", "braintree",
                    "authorizenet", "authorize_net", "payflow", "worldpay", "sagepay",
                    "klarna", "paypal", "amazon", "checkout_com", "checkoutcom",
                    "mollie", "square"]
_OFFLINE_FALLBACK = ["purchaseorder", "checkmo", "netterms", "paymentonaccount",
                     "payment_on_account", "companycredit", "banktransfer", "wirepayment",
                     "wiretransfer", "wire", "cashondelivery", "cashon", "moneyorder",
                     "payorder", "free", "zeropayment", "nopayment", "offlinepayment",
                     "offline", "check"]


def _kb_lists() -> tuple[list[str], list[str]]:
    """The (hosted, offline) substring vocabularies from the KB, lower-cased.
    Falls back to the built-in lists if the KB is absent. Never raises."""
    hosted, offline = [], []
    try:
        from ..knowledge import KB
        m = KB.platform_rules("magento") or {}
        hosted = [str(x).lower() for x in (m.get("hosted_gateways") or [])]
        offline = [str(x).lower() for x in (m.get("offline_payments") or [])]
    except Exception:
        pass
    return (hosted or _HOSTED_FALLBACK, offline or _OFFLINE_FALLBACK)


def classify_payment(code: str) -> str:
    """Classify a payment-method code as 'hosted', 'offline', or 'unknown'.

    Hosted is checked FIRST: a card gateway must never be mistaken for an offline
    method, because that would let the HTTP track attempt a card order it cannot
    complete. An unrecognised code returns 'unknown' — the caller decides how to
    treat it (the user is selecting from a real list, so they can confirm)."""
    c = str(code or "").lower().strip()
    if not c:
        return "unknown"
    hosted, offline = _kb_lists()
    if any(h in c for h in hosted):
        return "hosted"
    if any(o in c for o in offline):
        return "offline"
    return "unknown"


def _tag(code: str, title: str | None = None) -> dict:
    return {"code": code, "title": title or code, "kind": classify_payment(code)}


# --------------------------------------------------------------------------- #
# Live probe — authenticate a customer, read the cart's available methods.
# --------------------------------------------------------------------------- #
def _rest_candidates(rest_prefix: str | None) -> list[str]:
    """REST bases to try. A caller-supplied prefix (e.g. recording-derived
    '/uk/rest/uk/V1') wins; otherwise try the common Magento defaults."""
    if rest_prefix:
        return [rest_prefix]
    return ["/rest/V1", "/rest/default/V1", "/rest/all/V1"]


def _get_methods(session, base: str, pfx: str, auth: dict, timeout: int) -> list[dict]:
    try:
        r = session.get(base + pfx + "/carts/mine/payment-methods",
                        headers=auth, timeout=timeout)
    except Exception:
        return []
    if r.status_code >= 400:
        return []
    try:
        arr = json.loads(r.text or "[]")
    except Exception:
        return []
    out = []
    for m in (arr if isinstance(arr, list) else []):
        code = (m or {}).get("code")
        if code:
            out.append(_tag(code, m.get("title")))
    return out


def probe_payment_methods(base_url: str, username: str, password: str,
                          rest_prefix: str | None = None, sku: str | None = None,
                          timeout: int = 20) -> dict:
    """Live probe: mint a Magento customer token, read the cart's payment methods.

    Returns {"methods": [...], "source": "live"|"none", "rest_prefix": str,
    "note": str}. Magento only exposes cart-applicable methods to an
    authenticated cart, so valid credentials are required for the live path.
    Never raises."""
    import requests
    from .discovery import _norm_base
    try:
        from .generator import _clean_token
    except Exception:
        def _clean_token(txt):
            t = (txt or "").strip().strip('"').strip()
            return t if t and t[:1] not in "<{[" else None

    base = _norm_base(base_url)
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json",
                            "Accept": "application/json"})
    last_err = None
    for pfx in _rest_candidates(rest_prefix):
        try:
            r = session.post(base + pfx + "/integration/customer/token",
                             json={"username": username, "password": password},
                             timeout=timeout)
        except Exception as exc:
            last_err = "token request failed at %s: %s" % (pfx, exc)
            continue
        tok = _clean_token(r.text)
        if r.status_code >= 400 or not tok:
            last_err = "token status %s at %s" % (r.status_code, pfx)
            continue
        auth = {"Authorization": "Bearer %s" % tok}
        methods = _get_methods(session, base, pfx, auth, timeout)
        if not methods and sku:
            # The active cart may be empty — add one item and re-read, since some
            # methods only appear once the cart has contents / meets a minimum.
            try:
                session.post(base + pfx + "/carts/mine/items", headers=auth,
                             json={"cartItem": {"sku": str(sku), "qty": 1}},
                             timeout=timeout)
            except Exception:
                pass
            methods = _get_methods(session, base, pfx, auth, timeout)
        note = "" if methods else ("Authenticated, but the cart returned no payment "
                                   "methods — add an item to the cart or pass a SKU.")
        return {"methods": methods, "source": "live", "rest_prefix": pfx, "note": note}
    return {"methods": [], "source": "none",
            "note": last_err or "Could not authenticate to list payment methods."}


# --------------------------------------------------------------------------- #
# Recording fallback — methods observed in an uploaded recording.
# --------------------------------------------------------------------------- #
_HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD",
               "OPTIONS", "TRACE", "CONNECT"}


def methods_from_recording(flow: list | None) -> list[dict]:
    """Payment methods observed in a recording's flow: a captured
    payment-methods response, or the `"method":"X"` in a place-order body.
    Best-effort, de-duplicated, never raises."""
    out, seen = [], set()

    def _add(code, title=None):
        c = str(code or "").strip()
        if c and c.lower() not in seen and len(c) > 2:
            seen.add(c.lower())
            out.append(_tag(c, title))

    for s in (flow or []):
        if not isinstance(s, dict):
            continue
        path = str(s.get("path") or "").lower()
        body = s.get("resp") or s.get("response") or s.get("body")
        if "payment-methods" in path and body:
            try:
                arr = json.loads(body if isinstance(body, str) else json.dumps(body))
                for m in (arr if isinstance(arr, list) else []):
                    _add((m or {}).get("code"), (m or {}).get("title"))
            except Exception:
                pass
    # explicit `"method":"X"` in a recorded BODY (the place-order payload).
    # Scoped to bodies on purpose: every flow step carries its own "method" key
    # holding the HTTP verb, so searching the serialised step reports POST and
    # GET as payment methods.
    for s in (flow or []):
        if not isinstance(s, dict):
            continue
        for part in (s.get("req"), s.get("body"), s.get("resp"),
                     s.get("response"), s.get("post_data")):
            if not part:
                continue
            text = part if isinstance(part, str) else json.dumps(part)
            for m in re.findall(r'"method"\s*:\s*"([A-Za-z0-9_]+)"', text):
                if m.upper() in _HTTP_VERBS:
                    continue          # an HTTP verb, not a payment method
                _add(m)
    return out


def list_payment_methods(base_url: str | None = None, username: str | None = None,
                         password: str | None = None, rest_prefix: str | None = None,
                         recording_flow: list | None = None,
                         sku: str | None = None) -> dict:
    """Best available list of the target's payment methods for the config UI.

    Prefers a LIVE probe (needs base_url + credentials); falls back to methods
    seen in the recording. Always returns {"methods","source","note"} — an empty
    list with a note when nothing could be determined. Never raises."""
    result = {"methods": [], "source": "none", "note": ""}
    if base_url and username and password:
        try:
            result = probe_payment_methods(base_url, username, password,
                                           rest_prefix=rest_prefix, sku=sku)
        except Exception as exc:
            result = {"methods": [], "source": "none", "note": "probe error: %s" % exc}
    if not result.get("methods") and recording_flow:
        rec = methods_from_recording(recording_flow)
        if rec:
            prior = (result.get("note") or "").strip()
            note = "Showing methods seen in the recording."
            if prior:
                note = prior + " " + note
            return {"methods": rec, "source": "recording", "note": note}
    return result
