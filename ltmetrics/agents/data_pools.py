"""Multi-pool test data — detect per-transaction data pools from the Playwright
crawl + storefront GraphQL, bind each pool to the recording's transaction groups,
and let user-uploaded CSVs OVERRIDE detection.

Why this exists: a mature JMeter/BlazeMeter suite parameterizes each business
transaction from its OWN realistic data pool (a PLP-URL pool, a PDP-URL pool, a
manufacturer/brand pool, a weighted search-term pool, an auth pool). LT Metrics's default
is a single flat testdata.csv (one row per virtual user). This module adds the
richer model WITHOUT changing that default: it produces a machine-readable
`data-pools.json` manifest that the generator can consume for per-transaction
parameterization when present, and is simply ignored when absent.

What is / isn't detectable (be honest):
  * pdp           GraphQL products (sku+price) and crawled /buy//product/ URLs   -> DETECTABLE
  * plp / nav     crawled category / listing paths (+ ?p=N pagination)           -> DETECTABLE (sample-sized;
                                                                                    the default crawl is shallow)
  * manufacturer  crawled /manufacturer//brand/ paths                            -> DETECTABLE if a brand index is crawled
  * search        SEEDED from catalog vocabulary; real popularity WEIGHTS come    -> PARTIAL (upload the analytics CSV
                  from analytics (Event count), which a crawl cannot know            for real weighting)
  * auth          credentials are never crawlable                                 -> UPLOAD / register only

Everything is defensive: bad input yields empty pools, never raises.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

# Per-pool value cap so a huge catalog can't blow up memory / the manifest.
_POOL_CAP = 5000

# URL -> pool classification. Most-specific pools are tested first (a PDP URL often
# also ends in .html, which would otherwise look like a PLP). Defaults are broad and
# platform-agnostic; a KB override can extend these later without a code change.
_PATTERNS = {
    "pdp":          (r"/buy/", r"/product[s]?/", r"/p/", r"/dp/", r"/item[s]?/", r"/sku/"),
    "manufacturer": (r"/manufacturer[s]?/", r"/brand[s]?/", r"/vendor[s]?/", r"/make[r]?/"),
    "search":       (r"[?&](q|s|search|keyword|query|term)=",),
    "plp":          (r"[?&]p=\d+", r"/category/", r"/c/", r"/shop/", r"\.html($|\?)"),
}
# Order in which a URL is tested; first match wins. plp before nav; nav is the fallback.
_POOL_ORDER = ("pdp", "manufacturer", "search", "plp", "nav")

# The generated param/column each pool feeds when bound to a transaction.
_POOL_PARAM = {
    "pdp": "product_url", "plp": "plp_url", "manufacturer": "manufacturer_url",
    "nav": "nav_url", "search": "search_keyword", "auth": "username",
}

# Upload filename hints -> pool. Lets us auto-map page-urls.csv / pdp.csv / search-terms.csv.
_UPLOAD_HINTS = {
    "pdp": ("pdp", "product", "buy", "detail"),
    "plp": ("plp", "listing", "category", "categor"),
    "manufacturer": ("manufacturer", "brand", "vendor", "make"),
    "search": ("search", "term", "keyword", "query"),
    "nav": ("page-url", "page_url", "pageurls", "nav", "menu", "sitemap"),
    "auth": ("login", "user", "cred", "account", "testdata"),
}
# Column names that carry a selection WEIGHT (search popularity, hit count, etc.).
_WEIGHT_COLS = ("event count", "eventcount", "count", "weight", "hits", "frequency",
                "freq", "popularity", "views", "searches", "volume")


def _new_pool(kind: str) -> dict:
    return {"type": kind, "source": "", "weight_field": None, "columns": [], "values": []}


def _append(pool: dict, value: dict) -> None:
    if len(pool["values"]) < _POOL_CAP:
        pool["values"].append({k: v for k, v in value.items() if v not in (None, "")})


def classify_url(path: str) -> str:
    """Return the pool a URL/path belongs to (pdp/manufacturer/search/plp/nav)."""
    p = str(path or "")
    for kind in ("pdp", "manufacturer", "search"):
        if any(re.search(rx, p, re.I) for rx in _PATTERNS[kind]):
            return kind
    if any(re.search(rx, p, re.I) for rx in _PATTERNS["plp"]):
        return "plp"
    return "nav"


def _pool_for_upload(filename: str, columns) -> str:
    """Infer which pool an uploaded CSV feeds, from its filename then its columns."""
    fn = (filename or "").lower()
    for pool, hints in _UPLOAD_HINTS.items():
        if any(h in fn for h in hints):
            return pool
    cols = " ".join(str(c).lower() for c in (columns or []))
    if "username" in cols or "password" in cols:
        return "auth"
    if "search" in cols or "term" in cols or "keyword" in cols:
        return "search"
    if "url" in cols or "path" in cols:
        return "nav"          # generic URL list -> navigation pool
    return ""


def _read_csv_pool(path: str):
    """Read an uploaded CSV -> (rows[list[dict]], weight_field_or_None, columns)."""
    rows, weight_field, columns = [], None, []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as fh:
            sniff = fh.read(2048)
            fh.seek(0)
            has_header = bool(re.search(r"[A-Za-z]", sniff.splitlines()[0] if sniff else ""))
            reader = csv.reader(fh)
            first = next(reader, None)
            if first is None:
                return rows, None, columns
            if has_header and any(not _looks_like_url_or_data(c) for c in first):
                columns = [c.strip() for c in first]
            else:
                columns = ["value"] * len(first)
                rows.append(dict(zip(columns, [c.strip() for c in first])))
            for col in columns:              # find a weight column (search popularity)
                if col.lower() in _WEIGHT_COLS:
                    weight_field = col
                    break
            for rec in reader:
                if not rec:
                    continue
                rows.append({columns[i] if i < len(columns) else "col%d" % i: (rec[i].strip()
                             if i < len(rec) else "") for i in range(len(rec))})
                if len(rows) >= _POOL_CAP:
                    break
    except Exception:
        return rows, weight_field, columns
    return rows, weight_field, columns


def _looks_like_url_or_data(cell: str) -> bool:
    c = (cell or "").strip()
    return c.startswith("/") or c.startswith("http") or ("@" in c)


def detect(discovery: dict | None, flow_steps: list | None = None,
           live_products: dict | None = None, uploads: dict | None = None,
           target_url: str = "") -> dict:
    """Build named data pools from the crawl + GraphQL, override with uploads, and
    bind each pool to the recording's transaction groups. Returns the manifest dict
    (also the shape written to data-pools.json). Never raises."""
    pools = {k: _new_pool(k) for k in ("nav", "plp", "pdp", "manufacturer", "search", "auth")}
    notes = []

    # 1) Crawl pages -> classify each discovered URL into a pool.
    for pg in ((discovery or {}).get("pages") or []):
        path = (pg or {}).get("path") or ""
        if not path:
            continue
        kind = classify_url(path)
        _append(pools[kind], {"url": path, "name": pg.get("name")})
    for k, pool in pools.items():
        if pool["values"] and not pool["source"]:
            pool["source"] = "crawl"

    # 2) Storefront GraphQL products -> the PDP/product pool (real, in-stock, priced).
    gp = ((live_products or {}).get("products")) or []
    for pr in gp:
        sku = str((pr or {}).get("sku") or "")
        if not sku:
            continue
        _append(pools["pdp"], {"sku": sku, "price": pr.get("price"),
                               "currency": pr.get("currency"), "name": pr.get("name")})
    if gp:
        pools["pdp"]["source"] = ("graphql+crawl" if pools["pdp"]["source"] == "crawl"
                                  else "graphql")

    # 3) Seed the search pool from catalog vocabulary if nothing better is present.
    if not pools["search"]["values"]:
        seed = []
        se = (discovery or {}).get("search_endpoint") or {}
        for s in (se.get("samples") or se.get("terms") or []):
            seed.append(str(s))
        for pr in gp[:50]:                     # product names are decent seed terms
            if pr.get("name"):
                seed.append(str(pr["name"]))
        for t in dict.fromkeys(x for x in seed if x):   # de-dup, keep order
            _append(pools["search"], {"search_keyword": t})
        if pools["search"]["values"]:
            pools["search"]["source"] = "crawl-seed"
            notes.append("search pool is SEEDED from catalog vocabulary — upload your "
                         "analytics search-terms CSV (with an Event count / weight column) "
                         "for realistic query popularity.")

    # 4) Uploads OVERRIDE detection (the user's curated pool always wins).
    for up_name, up_path in (uploads or {}).items():
        rows, wfield, cols = _read_csv_pool(up_path)
        if not rows:
            continue
        pool = _pool_for_upload(up_name, cols) or _pool_for_upload(up_name, [])
        if not pool:
            notes.append("uploaded '%s' could not be mapped to a pool (unrecognized "
                         "filename/columns) — skipped." % up_name)
            continue
        pools[pool] = {"type": pool, "source": "upload:%s" % Path(str(up_path)).name,
                       "weight_field": wfield, "columns": cols, "values": rows[:_POOL_CAP]}
        if wfield:
            notes.append("pool '%s' uses weighted selection from column '%s'." % (pool, wfield))

    # 5) Bind each transaction group in the recording to a pool by URL pattern.
    bindings = _bind(pools, flow_steps)

    return {
        "target": target_url or (discovery or {}).get("base_url") or "",
        "pools": {k: _summarize(v) for k, v in pools.items() if v["values"]},
        "bindings": bindings,
        "coverage": {k: len(v["values"]) for k, v in pools.items() if v["values"]},
        "notes": notes,
    }


def _bind(pools: dict, flow_steps: list | None) -> list:
    """Map each recorded transaction group to the pool its URLs belong to."""
    out, seen = [], set()
    for s in (flow_steps or []):
        grp = (s or {}).get("group") or (s or {}).get("label") or ""
        path = (s or {}).get("path") or ""
        if not grp or grp in seen:
            continue
        kind = classify_url(path)
        if not pools.get(kind, {}).get("values"):
            continue
        seen.add(grp)
        out.append({"group": grp, "pool": kind, "param": _POOL_PARAM.get(kind, "value"),
                    "weighted": bool(pools[kind].get("weight_field"))})
    return out


def _summarize(pool: dict) -> dict:
    """Compact, manifest-safe view of a pool: metadata + a small sample of values."""
    return {
        "type": pool["type"], "source": pool["source"],
        "weight_field": pool.get("weight_field"),
        "columns": pool.get("columns") or [],
        "count": len(pool["values"]),
        "sample": pool["values"][:10],
        "param": _POOL_PARAM.get(pool["type"], "value"),
    }


def weighted_values(pool: dict) -> list:
    """Expand a pool into a flat selection list, repeating rows by their weight column
    so a simple random.choice() reproduces the recorded popularity. Falls back to the
    raw values when there is no weight column. Caps total expansion at _POOL_CAP."""
    vals = pool.get("values") or []
    wf = pool.get("weight_field")
    if not wf:
        return list(vals)
    out = []
    # Normalize weights so the smallest becomes ~1 and the list stays bounded.
    weights = []
    for v in vals:
        try:
            weights.append(max(0.0, float(str(v.get(wf, 0)).replace(",", ""))))
        except (TypeError, ValueError):
            weights.append(0.0)
    base = min([w for w in weights if w > 0] or [1.0])
    for v, w in zip(vals, weights):
        reps = int(round(w / base)) if base else 1
        out.extend([v] * max(1, min(reps, 200)))       # cap per-row repeats
        if len(out) >= _POOL_CAP:
            break
    return out[:_POOL_CAP] or list(vals)
