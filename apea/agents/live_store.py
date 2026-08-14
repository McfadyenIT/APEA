"""Live-store provider — the seam that makes the Test Data Generator's validation
and the Application KB's crawl REAL, by talking to the running store.

Two capabilities, both platform-adapter-based (Magento implemented; others degrade):

  discover_products(...)   READ-ONLY. Find real, IN-STOCK, PRICED products (valid
                           offers) via the storefront GraphQL API — never the
                           admin-scoped REST /V1/products (that 401s for anyone
                           without an integration token). Safe to run.

  register_customers(...)  WRITE. Create real customer accounts via Magento's PUBLIC
                           self-registration endpoint (POST /V1/customers) with a
                           valid address. HAS SIDE EFFECTS — creates accounts on the
                           store — so callers must opt in explicitly and point at a
                           TEST/STAGING store only.

Everything is defensive: short timeouts, all network errors caught, never raises.
Returns empty/degraded results when the store isn't reachable or the platform isn't
supported, so the Test Data Generator falls back to its offline backbone.
"""
from __future__ import annotations

import json as _json
import re


def _mag_prefixes(base_url: str, store: str):
    base = (base_url or "").rstrip("/")
    st = (store or "").strip("/")
    if st:
        return f"{base}/{st}/rest/{st}/V1", f"{base}/{st}/graphql"
    return f"{base}/rest/V1", f"{base}/graphql"


def store_from_paths(paths) -> str:
    """Best-effort store-view code from recorded REST URLs (/rest/<store>/V1)."""
    for p in (paths or []):
        m = re.search(r"/rest/([^/]+)/V1", str(p))
        if m:
            return m.group(1)
    return ""


# --------------------------------------------------------------------------- #
# READ-ONLY: real, in-stock, priced products via storefront GraphQL.
# --------------------------------------------------------------------------- #
def discover_products(base_url: str, search: str = "", count: int = 20,
                      store: str = "", platform: str = "magento",
                      timeout: int = 15) -> dict:
    """Return {'products': [{sku, price, currency, name}], 'source', 'note'}.
    Only products that are IN_STOCK with a price > 0 (a valid offer)."""
    if platform != "magento":
        return {"products": [], "source": "unsupported",
                "note": "live product discovery not implemented for '%s' yet" % platform}
    try:
        import requests
        _, gql = _mag_prefixes(base_url, store)
        terms = [t for t in [search, "the", "a", "1"] if t][:4]   # a few broad probes
        found, seen = [], set()
        query = ("query($q:String!,$n:Int!){products(search:$q,pageSize:$n){items{"
                 "sku name stock_status price_range{minimum_price{final_price{value currency}}}"
                 "}}}")
        for q in terms:
            if len(found) >= count:
                break
            try:
                r = requests.post(gql, json={"query": query, "variables": {"q": q, "n": count}},
                                  timeout=timeout)
                data = (r.json() if r.status_code < 400 else {}) or {}
            except Exception:
                continue
            items = (((data.get("data") or {}).get("products") or {}).get("items")) or []
            for it in items:
                sku = str(it.get("sku") or "").strip()
                if not sku or sku in seen:
                    continue
                if str(it.get("stock_status") or "").upper() != "IN_STOCK":
                    continue
                fp = (((it.get("price_range") or {}).get("minimum_price") or {}).get("final_price")) or {}
                price = fp.get("value")
                if not price or float(price) <= 0:
                    continue
                seen.add(sku)
                found.append({"sku": sku, "price": float(price),
                              "currency": fp.get("currency"), "name": it.get("name")})
                if len(found) >= count:
                    break
        return {"products": found, "source": "graphql" if found else "graphql (none found)",
                "note": "" if found else "no in-stock, priced products returned by GraphQL "
                        "search (check the store/search term, or the store may block GraphQL)"}
    except Exception as exc:
        return {"products": [], "source": "error", "note": "live product discovery error: %s" % exc}


# --------------------------------------------------------------------------- #
# WRITE (opt-in): create real customer accounts via public self-registration.
# --------------------------------------------------------------------------- #
def _address_body(address: dict) -> dict:
    a = address or {}
    reg = {}
    if a.get("region"):
        reg["region"] = a.get("region")
    if a.get("region_code"):
        reg["region_code"] = a.get("region_code")
    if a.get("region_id") not in (None, ""):
        try:
            reg["region_id"] = int(a["region_id"])
        except Exception:
            pass
    street = a.get("street")
    if isinstance(street, str):
        street = [street]
    return {
        "firstname": a.get("firstname") or "Load",
        "lastname": a.get("lastname") or "Test",
        "street": street or ["1 Test Way"],
        "city": a.get("city") or "New York",
        "country_id": a.get("country_id") or "US",
        "postcode": a.get("postcode") or "10001",
        "telephone": a.get("telephone") or "1234567890",
        "region": reg or None,
        "default_billing": True, "default_shipping": True,
    }


def can_authenticate(base_url: str, email: str, password: str, store: str = "",
                     timeout: int = 15) -> bool:
    """True only if this account can obtain a real REST customer token. New accounts
    on a store that requires email confirmation CANNOT — and would break the run."""
    try:
        import requests
        rest, _ = _mag_prefixes(base_url, store)
        r = requests.post(rest + "/integration/customer/token",
                          json={"username": email, "password": password}, timeout=timeout)
        if r.status_code >= 400:
            return False
        tok = (r.text or "").strip().strip('"').strip()
        return bool(tok) and tok[:1] not in "<{[" and len(tok) >= 20
    except Exception:
        return False


def register_customer(base_url: str, email: str, password: str, address: dict,
                      store: str = "", platform: str = "magento", timeout: int = 20) -> dict:
    """Create ONE customer via Magento's public POST /V1/customers, then VERIFY it can
    actually authenticate (get a REST token) — because an account that can't log in is
    worse than none (it silently breaks the load run). Returns {ok, id, email, error}.
    ok is True only when the account exists AND can authenticate. WRITE. Never raises."""
    if platform != "magento":
        return {"ok": False, "error": "registration not implemented for '%s'" % platform}
    try:
        import requests
        rest, _ = _mag_prefixes(base_url, store)
        addr = _address_body(address)
        body = {"customer": {"email": email,
                             "firstname": addr["firstname"], "lastname": addr["lastname"],
                             "addresses": [addr]},
                "password": password}
        r = requests.post(rest + "/customers", json=body, timeout=timeout)
        txt = (r.text or "")[:200]
        already = "already" in txt.lower() and "exist" in txt.lower()
        created = r.status_code < 400
        if not (created or already):
            return {"ok": False, "email": email, "error": "HTTP %s %s" % (r.status_code, txt)}
        cid = None
        if created:
            try:
                cid = (r.json() or {}).get("id")
            except Exception:
                cid = None
        # VERIFY: only accept accounts that can actually authenticate.
        usable = can_authenticate(base_url, email, password, store=store, timeout=timeout)
        return {"ok": usable, "id": cid, "email": email, "existing": already,
                "error": None if usable else ("account created but cannot obtain a REST "
                         "token — the store likely requires email confirmation")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "email": email}


def register_customers(base_url: str, creds: list, address: dict, store: str = "",
                       platform: str = "magento", limit: int = 100) -> dict:
    """Register up to `limit` accounts. Returns {created:[(email,pw)], errors:[...],
    note}. WRITE — opt-in only. Never raises."""
    created, errors = [], []
    try:
        for (email, pw) in (creds or [])[:limit]:
            res = register_customer(base_url, email, pw, address, store=store, platform=platform)
            if res.get("ok"):
                created.append((email, pw))
            else:
                errors.append({"email": email, "error": res.get("error")})
    except Exception as exc:
        errors.append({"error": str(exc)[:200]})
    return {"created": created, "errors": errors,
            "note": "registered %d account(s); %d error(s)" % (len(created), len(errors))}
