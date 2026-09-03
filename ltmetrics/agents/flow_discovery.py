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


# What a call DOES, read from its path. The order matters and is the order the
# journey has always used: "cart/add" is an Add to Cart before "checkout/cart"
# is a View Cart, and both before the catch-all "/checkout".
#
# Each stage also says what it proves happened, so the journey can still name
# itself ("Checkout", "Quote Submission") from the same pass.
_STAGE_RULES = (
    ("Login",               ("account/login", "loginpost"),                     ""),
    ("Search",              ("catalogsearch", "q=", "/search"),                 ""),
    ("Product View",        ("/buy/", "/product/", "catalog/product/view"),     ""),
    ("Add to Cart",         ("cart/add", "carts/mine/items", "add-to-cart",
                             "add_to_cart"),                                    "cart"),
    ("View Cart",           ("checkout/cart",),                                 "cart"),
    ("Quote",               ("negotiable", "requestforquote", "/rfq",
                             "quote/save", "quotes/mine"),                      "quote"),
    ("Shipping",            ("estimate-shipping", "shipping-information",
                             "shipping-method"),                                "checkout"),
    ("Payment / Place Order", ("payment-information", "set-payment",
                               "secureaccept", "placeorder", "place-order"),    "order"),
    ("Order Confirmation",  ("onepage/success", "checkout/success",
                             "order-received"),                                 "order"),
    ("Checkout",            ("/checkout",),                                     "checkout"),
)


def _kb_rules():
    """(stage rules, every-page patterns) from the KB, falling back to the tuple
    above. A YAML that fails to load must not take the classifier with it."""
    try:
        from ltmetrics.knowledge.kb import KB
        g = KB.platform_rules("generic") or {}
        rules = []
        for r in (g.get("journey_stages") or []):
            stage = (r or {}).get("stage")
            match = (r or {}).get("match") or []
            if stage and match:
                rules.append((stage, tuple(match), r.get("proves") or "",
                              bool(r.get("milestone", True))))
        if rules:
            return rules, tuple(g.get("every_page_calls") or ())
    except Exception:
        pass
    return [(n, m, pr, True) for n, m, pr in _STAGE_RULES], ()


def stage_of_step(step, milestones_only: bool = False) -> tuple:
    """(stage name, what it proves) for one recorded request, or ("", "").

    The single place a URL is turned into a business step. Two callers rely on
    it -- the recorded journey and the traffic groups -- and they must agree.

    `milestones_only` drops the supporting calls. A cart-totals read is a cart
    call worth grouping, but naming it in the journey put "Cart" after
    "Shipping" on a store that reads totals late.
    """
    p = ((step or {}).get("path") or "").lower()
    if (step or {}).get("login") or p.endswith("/login"):
        return "Login", ""
    rules, _ = _kb_rules()
    for name, needles, proves, milestone in rules:
        if milestones_only and not milestone:
            continue
        if any(n in p for n in needles):
            return name, proves
    return "", ""


def is_every_page_call(step) -> bool:
    """A call the store makes on every page whatever the shopper is doing.

    Real traffic, still sent, but not a step -- so it is grouped apart from the
    business calls the rules could not name. One bucket holding both could not
    be turned down safely: skipping the background traffic skipped the address
    picker with it.
    """
    p = ((step or {}).get("path") or "").lower()
    _, every_page = _kb_rules()
    return any(f in p for f in every_page)


def _is_useful_group_name(name: str) -> bool:
    """A group name earns its place by meaning something to a reader.

    Recorders that do not label transactions fall back to the URL, and one that
    labels nothing at all leaves a single wrapper around the whole session. In
    both cases the name says nothing about what the step does.
    """
    n = (name or "").strip()
    if not n:
        return False
    if "://" in n or n.startswith(("http", "/")) or n.count("/") >= 2:
        return False                       # it is a URL, not a business step
    return n.lower() not in {"test", "tests", "scenario", "recording", "session",
                             "thread group", "default", "untitled", "flow"}


def api_call_groups(flow: list) -> list:
    """Ordered business groups (JMeter Throughput-Controller style), with the
    request count per group. Returns [{"name", "count", "derived"}] in
    first-seen order; steps that belong to no group are omitted.

    Taken from the recording's own `transaction:` wrappers when those say
    something. When they do not -- a single wrapper named "Test", or labels that
    are just URLs -- the group is worked out from what each call does, so the
    panel offers the real steps of the journey instead of one slider for
    everything.
    """
    recorded = _count_groups((s.get("group") or "").strip() for s in (flow or []))
    useful = [g for g in recorded if _is_useful_group_name(g["name"])]
    if len(useful) >= 2:
        return [dict(g, derived=False) for g in recorded]

    def _label(step):
        return (stage_of_step(step)[0]
                or (_EVERY_PAGE if is_every_page_call(step) else _OTHER))

    derived = _count_groups(_label(s) for s in (flow or []))
    named = [g for g in derived if g["name"] not in (_OTHER, _EVERY_PAGE)]
    if not named:
        # Nothing recognisable either way: better one honest group than none.
        return [dict(g, derived=False) for g in recorded]
    # The two remainders go last: they are what is left, not steps of the
    # journey, and they are kept apart so the background traffic can be turned
    # down without taking an unnamed business call with it.
    rest = [g for g in derived if g["name"] in (_OTHER, _EVERY_PAGE)]
    rest.sort(key=lambda g: g["name"] == _EVERY_PAGE)
    return [dict(g, derived=True) for g in named + rest]


# Everything the rules cannot name. Named plainly, and counted, so the panel
# adds up to the journey rather than quietly showing a fraction of it.
_OTHER = "Unrecognised calls"
# Kept apart from _OTHER on purpose: this one is safe to turn down, and the
# other is not. Both names were changed twice because the operator had to ask
# what they meant: a label on this screen has to say what the thing IS, not
# where it happens ("Every-page calls") or that it is a leftover ("Other").
_EVERY_PAGE = "Background traffic"


def _count_groups(names) -> list:
    order, counts = [], {}
    for g in names:
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
        # Same classifier the traffic groups use, so the journey and the panel
        # can never name the same call two different things.
        stage, proves = stage_of_step(s, milestones_only=True)
        if not stage:
            continue
        add(stage)
        has_cart = has_cart or proves == "cart"
        has_quote = has_quote or proves == "quote"
        has_checkout = has_checkout or proves == "checkout"
        has_order = has_order or proves == "order"
    if not milestones:
        milestones = ["Recorded steps"]
    if has_quote:
        name = "Quote Submission"
    elif has_order or has_checkout or has_cart:
        name = "Checkout"
    else:
        name = "Recorded Journey"
    return name, milestones
