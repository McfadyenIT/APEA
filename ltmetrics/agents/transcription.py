"""Recorder / Transcription agent.

Fuses multiple recordings of the SAME user journey — an uploaded BlazeMeter
JMX/HAR/YAML recording, the Playwright manual/auto crawl capture, and any Locust
recording — into ONE clean, de-duplicated, ordered flow for script generation.

Strategy (deterministic, safe):
  * Clean each source (drop assets/analytics/duplicates).
  * Pick the most COMPLETE source as the backbone (the one that reaches furthest
    into checkout / an order).
  * ENRICH backbone steps with bodies / headers / flags found in the other
    sources (so a browser capture fills in what a JMeter recording missed and
    vice-versa) — never reordering the backbone.
  * If the backbone never reaches checkout but another source does, append that
    source's checkout tail.
  * Optional AI pass (Claude, if a key is set) removes leftover noise and fixes
    ordering — by SELECTING/reordering existing steps only, never inventing URLs.

Everything runs at build time, never inside a Locust worker.
"""
from __future__ import annotations

import re

_ASSET_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|woff2?|ttf|ico|map|mp4)(\?|$)", re.I)
_NOISE = ("google-analytics", "googletagmanager", "/gtm", "facebook", "/collect",
          "hotjar", "doubleclick", "/beacon", "newrelic", "/mixpanel", "segment.io",
          "/pixel", "cdn-cgi", "recaptcha/api2")
_ORDER_SIGNALS = ("payment-information", "placeorder", "place-order", "onepage/success",
                  "checkout/success", "saveorder", "submitorder", "negotiable")
_CHECKOUT_START = ("cart/add", "carts/mine/items", "checkout/cart", "add-to-cart",
                   "checkout", "estimate-shipping", "shipping-information")


def _norm(path: str) -> str:
    """Normalize a path for de-dup: drop the query and volatile cache-buster ids."""
    p = (path or "").split("?")[0]
    p = re.sub(r"/\d{6,}", "/#", p)         # long numeric ids
    return p.rstrip("/") or "/"


def clean(flow: list) -> list:
    """Drop asset/analytics/OPTIONS/duplicate requests from one flow."""
    out, seen = [], set()
    for s in flow or []:
        method = (s.get("method") or "GET").upper()
        path = s.get("path") or "/"
        low = path.lower()
        if method == "OPTIONS" or _ASSET_RE.search(low) or any(n in low for n in _NOISE):
            continue
        key = (method, _norm(path), bool(s.get("body")))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _score(flow: list) -> int:
    """Higher = more complete (reaches deeper into checkout / an order)."""
    s = len(flow)
    for st in flow:
        low = (st.get("path") or "").lower()
        if any(k in low for k in _ORDER_SIGNALS):
            s += 1000
    return s


def _reaches_order(flow: list) -> bool:
    return any(any(k in (st.get("path") or "").lower() for k in _ORDER_SIGNALS)
               for st in flow)


def fuse(sources: list, base_url: str | None = None, use_ai: bool = True) -> dict:
    """Combine flow-bearing sources into one clean flow. `sources` is a list of
    dicts with at least a 'flow' key (and optional 'source' label)."""
    flows = []
    for s in sources or []:
        f = clean(s.get("flow") or [])
        if f:
            flows.append((s.get("source", "?"), f))
    if not flows:
        return {"source": "fused", "flow": [], "endpoints": [], "sources": []}

    flows.sort(key=lambda nf: _score(nf[1]), reverse=True)
    backbone = list(flows[0][1])

    # index backbone for enrichment + membership
    idx = {}
    for i, st in enumerate(backbone):
        idx.setdefault((st.get("method", "GET"), _norm(st.get("path", "/"))), i)

    # 1) enrich backbone steps with bodies / flags from the other sources
    for _, f in flows[1:]:
        for st in f:
            key = (st.get("method", "GET"), _norm(st.get("path", "/")))
            if key in idx:
                b = backbone[idx[key]]
                if not b.get("body") and st.get("body"):
                    b["body"] = st["body"]
                for flag in ("xhr", "json", "rest"):
                    b[flag] = b.get(flag) or st.get(flag)
                if st.get("asserts") and not b.get("asserts"):
                    b["asserts"] = st["asserts"]

    # 2) if the backbone never reaches an order but a secondary source does,
    #    append that source's checkout tail (from its first checkout step on).
    if not _reaches_order(backbone):
        for _, f in flows[1:]:
            if _reaches_order(f):
                start = next((i for i, st in enumerate(f)
                              if any(k in (st.get("path") or "").lower()
                                     for k in _CHECKOUT_START)), 0)
                for st in f[start:]:
                    key = (st.get("method", "GET"), _norm(st.get("path", "/")))
                    if key not in idx:
                        idx[key] = len(backbone)
                        backbone.append(st)
                break

    flow = backbone
    if use_ai:
        flow = _ai_clean(flow, base_url) or flow

    endpoints = [{"method": "GET", "path": s["path"], "name": s.get("label") or s["path"]}
                 for s in flow if (s.get("method") or "GET").upper() == "GET"]
    return {"source": "fused", "flow": flow, "endpoints": endpoints,
            "sources": [n for n, _ in flows]}


def _ai_clean(flow: list, base_url):
    """Optional Claude pass: reorder / drop noise by SELECTING existing steps.
    Returns a new flow list or None. Never fabricates URLs. Fully guarded."""
    try:
        from . import llm
        if not llm.available() or len(flow) < 4:
            return None
        import json
        listing = [{"i": i, "m": s.get("method"), "p": (s.get("path") or "")[:120],
                    "body": bool(s.get("body"))} for i, s in enumerate(flow)]
        prompt = (
            "These are recorded HTTP steps of ONE user journey for load testing on "
            + str(base_url) + ". Produce a clean sequential journey: drop analytics/"
            "duplicate/noise steps and fix obvious ordering so it reads login → "
            "browse → cart → checkout → payment → order (only if those exist). "
            "Return JSON {\"order\":[indices in final order]}. Use ONLY the given "
            "indices; do not invent any.\n\nSTEPS:\n" + json.dumps(listing))
        out = llm.json_call(prompt, system="You clean recorded load-test journeys "
                            "by selecting and ordering existing steps only.",
                            max_tokens=900)
        order = (out or {}).get("order")
        if not isinstance(order, list) or not order:
            return None
        picked = [flow[i] for i in order if isinstance(i, int) and 0 <= i < len(flow)]
        # sanity: keep only if it retained a reasonable fraction of the steps
        if len(picked) < max(3, len(flow) // 3):
            return None
        return picked
    except Exception:
        return None
