"""Business-flow discovery — turn the parsed recording into a named user journey
with ordered milestones, fold it into the crawl-based discovery result, and (as a
last resort) reconstruct an ordered HTTP flow from a malformed recording via LLM.

Split out of recording.py (behavior-preserving, verbatim moves). Depends on
`filter` (asset detection) and `normalizer` (labels); never imports `parser`, so
`parser` can safely import `_ai_yaml_flow` from here without a cycle.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .filter import is_asset
from .normalizer import _short_label


def _ai_yaml_flow(text: str) -> list:
    """Claude fallback: extract an ordered HTTP flow from a malformed recording.
    Returns [] without an API key or on any failure."""
    try:
        from . import llm
        if not llm.available() or not text.strip():
            return []
        out = llm.json_call(
            "Extract the ordered HTTP requests from this BlazeMeter/Taurus/JMeter "
            "recording. Ignore Selenium UI actions and static assets. Return JSON "
            "{\"requests\":[{\"method\":\"POST\",\"path\":\"/uk/...\",\"body\":\"...\"}]}, "
            "paths root-relative, body only for POST/PUT.\n\nRECORDING (truncated):\n"
            + text[:12000],
            system="You convert recordings into a clean ordered HTTP request list.",
            max_tokens=2000) or {}
        reqs = out.get("requests")
        if not isinstance(reqs, list):
            return []
        flow = []
        for r in reqs:
            if not isinstance(r, dict):
                continue
            path = str(r.get("path") or "")
            if not path:
                continue
            u = urlparse(path)
            p = (u.path or path) + (f"?{u.query}" if u.query else "")
            if not p.startswith("/"):
                p = "/" + p
            if is_asset(p):
                continue
            method = (r.get("method") or "GET").upper()
            body = r.get("body")
            flow.append({"method": method, "path": p,
                         "label": _short_label(method, u.path or p),
                         "body": body if isinstance(body, (dict, str)) else None,
                         "asserts": [], "xhr": True, "json": bool(body and "{" in str(body)),
                         "rest": "/rest/" in p})
        return flow
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# merge into discovery
# --------------------------------------------------------------------------- #
def merge_into_discovery(discovery: dict, rec: dict) -> dict:
    """Fold a recording into discovery: attach the ordered flow + browse pages."""
    if not rec:
        return discovery

    # Ordered flow drives a sequential, correlated Locust task.
    flow = rec.get("flow")
    if not flow and rec.get("endpoints"):
        flow = [{"method": e["method"], "path": e["path"], "label": e["name"],
                 "body": None, "asserts": []} for e in rec["endpoints"]]
    if flow:
        discovery["flow"] = flow

    pages = discovery.setdefault("pages", [])
    existing = {p.get("path") for p in pages}
    for ep in rec.get("endpoints", []):
        if ep["method"] == "GET" and ep["path"] not in existing:
            pages.append({"name": ep["name"], "path": ep["path"], "method": "GET"})
            existing.add(ep["path"])

    if rec.get("base_url") and not discovery.get("reachable"):
        discovery["base_url"] = rec["base_url"]

    # Recorded browser (Selenium) actions, preserved for the analysis report
    # and richer parameterization — never used to drive the replayed HTTP flow.
    if rec.get("ui_steps"):
        discovery["ui_steps"] = rec["ui_steps"]

    # Split the workload into Browse (crawled journeys) + the RECORDED journey.
    # The recorded journey's name + steps are derived from what was ACTUALLY
    # captured — not a fixed login→search→cart→checkout template — so a browse,
    # search, quote, or account journey is represented faithfully.
    if flow:
        checkout_pct = int(discovery.get("checkout_pct", 30))
        name, steps = _derive_journey(flow)
        journeys = [j for j in (discovery.get("journeys") or []) if not j.get("recorded")]
        btotal = sum(j.get("weight", 0) for j in journeys) or 1
        for j in journeys:
            j["weight"] = max(1, round(j.get("weight", 0) * (100 - checkout_pct) / btotal))
        journeys.append({"name": name, "weight": checkout_pct, "steps": steps,
                         "recorded": True})
        discovery["journeys"] = journeys
        discovery["checkout_pct"] = checkout_pct

    discovery["recording"] = {"source": rec.get("source"),
                              "count": len(flow or []),
                              "error": rec.get("error")}
    return discovery


def api_call_groups(flow: list) -> list:
    """Ordered business groups (JMeter Throughput-Controller style) recovered from
    the recording's `transaction:` wrappers, with the request count per group.
    Returns [{"name", "count"}] in first-seen order; ungrouped steps are omitted."""
    order, counts = [], {}
    for s in (flow or []):
        g = (s.get("group") or "").strip()
        if not g:
            continue
        if g not in counts:
            order.append(g)
        counts[g] = counts.get(g, 0) + 1
    return [{"name": g, "count": counts[g]} for g in order]


def _derive_journey(flow: list) -> tuple:
    """Infer the recorded journey's name + ordered milestones from the captured
    requests, so the journey reflects the user's actual actions."""
    milestones, seen = [], set()

    def add(m):
        if m not in seen:
            seen.add(m)
            milestones.append(m)

    has_order = has_quote = has_cart = has_checkout = False
    for s in flow:
        p = (s.get("path") or "").lower()
        if s.get("login") or "account/login" in p or "loginpost" in p or p.endswith("/login"):
            add("Login")
        elif "catalogsearch" in p or "q=" in p or "/search" in p:
            add("Search")
        elif "/buy/" in p or "/product/" in p or "catalog/product/view" in p:
            add("Product View")
        elif ("cart/add" in p or "carts/mine/items" in p or "add-to-cart" in p
              or "add_to_cart" in p):
            add("Add to Cart"); has_cart = True
        elif "checkout/cart" in p:
            add("View Cart"); has_cart = True
        elif ("negotiable" in p or "requestforquote" in p or "/rfq" in p
              or "quote/save" in p or "quotes/mine" in p):
            add("Quote"); has_quote = True
        elif "estimate-shipping" in p or "shipping-information" in p or "shipping-method" in p:
            add("Shipping"); has_checkout = True
        elif ("payment-information" in p or "set-payment" in p or "secureaccept" in p
              or "placeorder" in p or "place-order" in p):
            add("Payment / Place Order"); has_order = True
        elif "onepage/success" in p or "checkout/success" in p or "order-received" in p:
            add("Order Confirmation"); has_order = True
        elif "/checkout" in p:
            add("Checkout"); has_checkout = True
    if not milestones:
        milestones = ["Recorded steps"]
    if has_quote:
        name = "Quote Submission"
    elif has_order or has_checkout or has_cart:
        name = "Checkout"
    else:
        name = "Recorded Journey"
    return name, milestones
