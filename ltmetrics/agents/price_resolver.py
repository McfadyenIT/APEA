"""Price-resolution auto-heal (platform-level).

Problem: on catalogs that price CLIENT-SIDE (e.g. a custom pricing service, or a
zero-catalog-price catalog like Radwell), Magento's REST catalog/cart API returns
`price: 0`, so an HTTP load test records £0 line items even though the storefront
shows a real price. LT Metrics should never accept a £0 product silently.

This module is the heal: when a run added items at price 0, it fetches the product
page over plain HTTP (`requests`, no browser needed) and extracts the REAL price
from the SERVER-RENDERED markup the storefront embeds for SEO — schema.org
`product-schema-price` / JSON-LD `Offer.price` / `itemprop=price` / `data-price-
amount`. Deterministic patterns first (KB-extensible); Claude is a fallback that
reads the page and extracts the price when the patterns miss.

Design (mirrors llm.py / repair.py): POST-RUN only, off the load hot path, never
raises. If the price is injected purely by JS AFTER render (not in the HTML),
plain HTTP can't see it — only the browser (Track B) can, and this returns None.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlparse

# Server-rendered price locations, most specific first. Platform-agnostic; a store
# that puts price elsewhere just needs a pattern added (KB-overridable).
_PRICE_PATTERNS = [
    r'product-schema-price[^>]*?value=["\']([0-9][0-9,]*\.?[0-9]*)',
    r'itemprop=["\']price["\'][^>]*content=["\']([0-9][0-9,]*\.?[0-9]*)',
    r'"@type"\s*:\s*"Offer"[\s\S]{0,300}?"price"\s*:\s*"?([0-9][0-9,]*\.?[0-9]*)',
    r'data-price-amount=["\']([0-9][0-9,]*\.?[0-9]*)',
    r'"final_price"\s*:\s*"?([0-9][0-9,]*\.?[0-9]*)',
]
# A .html product link on a search-results / listing page.
_PRODUCT_LINK_PATTERNS = [
    r'class="[^"]*product-item-link[^"]*"[^>]*href="([^"]+)"',
    r'href="([^"]+)"[^>]*class="[^"]*product-item-link',
    r'href="([^"]+\.html)"[^>]*class="[^"]*product',
    r'href="([^"]+\.html)"',
]


def _num(s: str) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return 0.0


def _extract_price(html: str, patterns=None) -> float:
    for pat in (patterns or _PRICE_PATTERNS):
        m = re.search(pat, html or "")
        if m:
            v = _num(m.group(1))
            if v > 0:
                return v
    return 0.0


def _kb_patterns():
    """Optional KB override/extension of the price patterns (browser_patterns.yaml
    -> price_patterns). Best-effort."""
    try:
        import yaml
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "knowledge" / "rules" / "browser_patterns.yaml"
        pats = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("price_patterns")
        return list(pats) + _PRICE_PATTERNS if pats else _PRICE_PATTERNS
    except Exception:
        return _PRICE_PATTERNS


def _pick_link(links, hints):
    """From candidate product links, prefer one that matches a hint (the ordered
    sku / product-id digits) so we price the RIGHT product, not just the first
    search result. Falls back to the first link."""
    links = list(dict.fromkeys(links))
    for h in (hints or []):
        d = re.sub(r"\D", "", str(h or ""))          # digits of sku / product id
        if len(d) >= 4:
            hit = next((l for l in links if d in l), None)
            if hit:
                return hit
    return links[0] if links else None


def _currency_near(html: str, price) -> str:
    """Currency symbol shown next to the price (so a UK £ store isn't mislabelled
    $ just because a '$' appears elsewhere in a script)."""
    try:
        num = str(price).split(".")[0]
        i = (html or "").find(num)
        window = html[max(0, i - 30): i + 30] if i >= 0 else (html or "")[:400]
        for sym in ("£", "€", "$"):
            if sym in window:
                return sym
    except Exception:
        pass
    return "£" if "£" in (html or "")[:20000] else ("$" if "$" in (html or "")[:20000] else "")


def resolve(base_url: str, search_term: str, store: str | None = None,
            pdp_url: str | None = None, match_hints=None, timeout: int = 15) -> dict | None:
    """Resolve the REAL price for a product. Prefer an explicit PDP url; else
    search the storefront for `search_term` and open the result that MATCHES the
    ordered sku / product id (not just the first). Returns {price, currency, url,
    source} or None. Never raises."""
    try:
        import requests
    except Exception:
        return None
    base = (base_url or "").rstrip("/")
    if not base:
        return None
    patterns = _kb_patterns()
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 (LT Metrics price-resolve)",
                             "Accept": "text/html,application/xhtml+xml"})
        # 1) locate a product page
        product_url = pdp_url
        if product_url and product_url.startswith("/"):
            product_url = base + product_url
        if not product_url and search_term:
            for path in ([f"/{store}/catalogsearch/result/?q=" if store else None,
                          "/catalogsearch/result/?q="]):
                if not path:
                    continue
                try:
                    r = sess.get(base + path + quote(str(search_term)), timeout=timeout)
                except Exception:
                    continue
                if r is None or r.status_code >= 400:
                    continue
                links = []
                for lp in _PRODUCT_LINK_PATTERNS:
                    links += [urljoin(r.url, x) for x in re.findall(lp, r.text or "")]
                chosen = _pick_link(links, match_hints)   # match the ORDERED product
                if chosen:
                    product_url = chosen
                    break
        if not product_url:
            return None
        # 2) read the product page and extract the server-rendered price
        pr = sess.get(product_url, timeout=timeout)
        if pr is None or pr.status_code >= 400:
            return None
        price = _extract_price(pr.text, patterns)
        source = "pdp html (schema/json-ld)"
        if price <= 0:
            price = _ai_extract(pr.text)          # Claude fallback (post-run only)
            source = "pdp html (AI)" if price > 0 else source
        if price <= 0:
            return None
        return {"price": price, "currency": _currency_near(pr.text, price),
                "url": product_url, "source": source}
    except Exception:
        return None


def _ai_extract(html: str) -> float:
    """Claude fallback: extract the product's price from the PDP HTML when the
    deterministic patterns miss. Returns 0.0 without an API key or on failure."""
    try:
        from . import llm
        if not llm.available() or not html:
            return 0.0
        # send a compact, price-relevant slice (keep the prompt small)
        low = html.lower()
        idx = low.find("price")
        snippet = html[max(0, idx - 1500): idx + 2500] if idx >= 0 else html[:4000]
        out = llm.json_call(
            "Extract the product's unit selling PRICE (the number a customer pays) "
            "from this product-page HTML fragment. Return ONLY JSON "
            '{"price": <number or null>, "currency": "<symbol or code>"}. '
            "Ignore VAT-inclusive, list/was, and quote-only prices; prefer the main "
            "selling price.\n\nHTML:\n" + snippet,
            system="You extract a single numeric price from HTML. Output strict JSON only.",
            max_tokens=120) or {}
        return _num(out.get("price")) if out.get("price") is not None else 0.0
    except Exception:
        return 0.0
