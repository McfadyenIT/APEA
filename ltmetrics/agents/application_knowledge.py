"""Application Knowledge Base — a per-target, platform-independent model of the
application under test.

LT Metrics's other modules reason about ONE recording. This module builds the broader
picture of the whole application — its navigation, business objects, API surface,
authentication and storage — as a machine contract (application-knowledge.json)
that can be REFRESHED independently of any single recording (via `refresh`).

Platform-independence is by construction: it uses LT Metrics's existing KB-driven platform
registry (ltmetrics.platforms + knowledge/rules/platform_rules.yaml) as the ADAPTER
layer, so a new platform (Adobe Commerce / commercetools / Mirakl / SFCC / Shopify /
SAP Commerce / custom) is taught by adding a KB block, not by editing this module.

This is the BACKBONE: it assembles everything discovery already knows and defines
the seam a live independent crawl plugs into later (to enrich business objects,
inventory, coupons, storage and feature flags) — without changing any caller.

Never raises; degrades to a minimal KB when discovery is sparse.

Contract (application-knowledge.json):
    {
      "target": str, "base_url": str, "platform": str, "adapter": str,
      "tech": [str], "domain": str, "generated_at": iso, "source": str,
      "navigation": {home, search, login, cart, checkout, categories[], products[]},
      "api_surface": {rest[], graphql[], ajax[], websocket[], other[]},
      "business_objects": {products, categories, customers, addresses, inventory,
                           coupons, promotions, shipping_methods, payment_methods},
      "auth": {type, login_url, token_endpoint, fields[]},
      "storage": {cookies[], local_storage[], session_storage[]},
      "feature_flags": {}, "coverage": {section: bool}, "notes": str
    }
"""
from __future__ import annotations

import re
from datetime import datetime


def _host(u: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(u or "").netloc or (u or "")
    except Exception:
        return u or ""


def _classify_api(path: str) -> str:
    p = (path or "").lower()
    if "graphql" in p:
        return "graphql"
    if p.startswith("ws://") or p.startswith("wss://") or "websocket" in p:
        return "websocket"
    if "/rest/" in p or "/v1/" in p or "/api/" in p or "/carts/mine" in p:
        return "rest"
    if any(k in p for k in ("ajax", "getitby", "section/load", "/index/index",
                            "checkout/setsession", "dataprovider")):
        return "ajax"
    return "other"


def _navigation(d: dict) -> dict:
    nav = {"home": None, "search": None, "login": None, "cart": None,
           "checkout": None, "registration": None, "orders": None,
           "categories": [], "products": []}
    se = d.get("search_endpoint") or {}
    if se.get("path") or se.get("url"):
        nav["search"] = se.get("path") or se.get("url")
    lf = d.get("login_form") or {}
    if lf.get("action"):
        nav["login"] = lf.get("action")
    for pg in (d.get("pages") or []):
        path = str(pg.get("path") or "")
        low = path.lower()
        if not path:
            continue
        if low in ("/", "") and not nav["home"]:
            nav["home"] = path
        elif "checkout" in low and "cart" not in low and not nav["checkout"]:
            nav["checkout"] = path
        elif "cart" in low and not nav["cart"]:
            nav["cart"] = path
        elif ("account/login" in low or low.endswith("/login")) and not nav["login"]:
            nav["login"] = path
        elif ("account/create" in low or "register" in low) and not nav["registration"]:
            nav["registration"] = path
        elif ("sales/order" in low or "/orders" in low) and not nav["orders"]:
            nav["orders"] = path
        elif any(k in low for k in ("category", "/catalog/", "manufacturer", "/plp")):
            nav["categories"].append(path)
        elif low.endswith(".html") or "/product/" in low or "/buy/" in low or "/p/" in low:
            nav["products"].append(path)
    nav["categories"] = nav["categories"][:50]
    nav["products"] = nav["products"][:50]
    return nav


def _api_surface(d: dict) -> dict:
    surf = {"rest": [], "graphql": [], "ajax": [], "websocket": [], "other": []}
    seen = set()
    steps = list(d.get("flow") or []) + [{"path": a.get("path") or a.get("url"),
                                          "method": a.get("method")}
                                         for a in (d.get("apis") or [])]
    for s in steps:
        path = s.get("path") or s.get("url") or ""
        method = (s.get("method") or "GET").upper()
        if not path:
            continue
        key = (method, str(path))
        if key in seen:
            continue
        seen.add(key)
        surf[_classify_api(path)].append({"method": method, "path": str(path)[:200]})
    for k in surf:
        surf[k] = surf[k][:80]
    return surf


def _business_objects(d: dict) -> dict:
    skus, categories = set(), set()
    for s in (d.get("flow") or []):
        body = s.get("body")
        bs = body if isinstance(body, str) else (str(body) if body else "")
        for m in re.finditer(r'"sku"\s*:\s*"([^"]+)"', bs or ""):
            skus.add(m.group(1))
        m2 = re.search(r'sku=([A-Za-z0-9\-_]+)', bs or "")
        if m2:
            skus.add(m2.group(1))
    for pg in (d.get("pages") or []):
        low = str(pg.get("path") or "").lower()
        if any(k in low for k in ("category", "/catalog/", "manufacturer")):
            categories.add(str(pg.get("name") or pg.get("path")))
    return {
        "products": {"discovered_skus": sorted(skus)[:50], "count": len(skus)},
        "categories": sorted(categories)[:50],
        "customers": {},          # enriched by a live crawl / test-data generator
        "addresses": {},
        "inventory": {},
        "coupons": [],
        "promotions": [],
        "shipping_methods": [],
        "payment_methods": [],
    }


def _auth(d: dict, platform: str) -> dict:
    lf = d.get("login_form") or {}
    auth = {"type": "session", "login_url": lf.get("action"),
            "token_endpoint": None, "fields": lf.get("fields") or []}
    try:
        from ..knowledge import KB
        rules = KB.platform_rules(platform) or {}
        tok = rules.get("token_endpoint")
        if tok:
            auth["token_endpoint"] = tok
            auth["type"] = "bearer"
        if rules.get("bearer_header"):
            auth["bearer_header"] = rules.get("bearer_header")
    except Exception:
        pass
    return auth


def build(discovery: dict | None, target_url: str = "",
          source: str = "recording+discovery") -> dict:
    """Assemble the Application Knowledge Base from discovery. Never raises."""
    try:
        d = discovery or {}
        base = target_url or d.get("base_url") or d.get("start_url") or ""
        try:
            from .. import platforms
            platform = platforms.detect(d, url=base) or "generic"
        except Exception:
            platform = "generic"
        nav = _navigation(d)
        surf = _api_surface(d)
        biz = _business_objects(d)
        auth = _auth(d, platform)
        coverage = {
            "navigation": bool(nav.get("home") or nav.get("checkout") or nav.get("search")),
            "api_surface": any(surf.get(k) for k in ("rest", "graphql", "ajax")),
            "business_objects": biz["products"]["count"] > 0 or bool(biz["categories"]),
            "auth": bool(auth.get("login_url") or auth.get("token_endpoint")),
            "storage": False,       # populated by a live crawl
            "feature_flags": False,
        }
        return {
            "target": _host(base),
            "base_url": d.get("base_url") or base,
            "platform": platform,
            "adapter": platform,        # the platform block IS the adapter
            "tech": d.get("tech") or [],
            "domain": d.get("domain"),
            "generated_at": datetime.utcnow().isoformat(),
            "source": source,
            "navigation": nav,
            "api_surface": surf,
            "business_objects": biz,
            "auth": auth,
            "storage": {"cookies": [], "local_storage": [], "session_storage": []},
            "feature_flags": {},
            "coverage": coverage,
            "notes": ("Assembled from discovery. An independent live crawl (refresh) "
                      "can enrich business objects, inventory, coupons, storage and "
                      "feature flags without changing any consumer of this contract."),
        }
    except Exception as exc:
        return {
            "target": _host(target_url), "base_url": target_url, "platform": "generic",
            "adapter": "generic", "source": "error", "generated_at": "",
            "navigation": {}, "api_surface": {}, "business_objects": {}, "auth": {},
            "storage": {}, "feature_flags": {}, "coverage": {},
            "notes": "application-knowledge build error: %s" % exc,
        }


def refresh(target_url: str, discovery: dict | None = None, adapter: str | None = None) -> dict:
    """Independent-refresh seam — rebuild the Application KB for a target WITHOUT a
    new recording. Today it re-assembles from the provided discovery; a per-platform
    live crawl adapter plugs in here later without changing callers."""
    return build(discovery or {}, target_url=target_url, source="refresh")
