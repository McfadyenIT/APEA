"""Business Data Dependency discovery — Browser-to-API business-flow fidelity.

LT Metrics's Discovery answers "which API calls?". This answers the harder question:
"which BUSINESS VALUES must be reproduced to faithfully replay the transaction at
the API level, and where do they originate?" — price, product, inventory,
promotion, tax, shipping, currency. Generic across platforms; nothing here is
hard-coded to Magento / CommerceTools / Mirakl / SFCC — the same mechanism
applies to any store, because it reasons about VALUES and their sources, not
about a specific vendor's endpoints.

Each business value is CLASSIFIED so the Generator knows how to source it rather
than blindly reproducing a recorded number:

    STATIC            a fixed constant safe to replay as-is
    PARAMETER         varies per user -> comes from the test-data CSV (e.g. sku)
    CORRELATION       server-issued, captured from an earlier response (cart id)
    RUNTIME_DERIVED   computed/looked-up at runtime, must NOT be a stale CSV value
    SERVER_GENERATED  minted by the server (order id)
    CLIENT_CALCULATED browser computes it (base+promo+tax+currency)
    BUSINESS_REFERENCE a stable business key (product id / part number)
    UNKNOWN           needs a human / AI look

First dimension implemented end-to-end: PRICE — including the Browser-vs-API
fidelity check that flags "price shown in the browser but 0 in the API replay",
which is the class of bug that motivated this whole capability.
"""
from __future__ import annotations

import re

# Value classifications (the Generator's sourcing decision).
STATIC = "STATIC"
PARAMETER = "PARAMETER"
CORRELATION = "CORRELATION"
RUNTIME_DERIVED = "RUNTIME_DERIVED"
SERVER_GENERATED = "SERVER_GENERATED"
CLIENT_CALCULATED = "CLIENT_CALCULATED"
BUSINESS_REFERENCE = "BUSINESS_REFERENCE"
UNKNOWN = "UNKNOWN"


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("£", "").replace("$", "")
                     .replace("€", "").strip())
    except Exception:
        return None


def _api_item_price(flow: dict):
    """The line-item price the API returned when the product was added to cart
    (this is what shows as £0 on a client-side-priced catalog)."""
    # Every price the cart reported, in order. The FIRST one is not the answer:
    # on a client-side-priced catalogue the REST add always lands at 0 and the
    # storefront cart-add prices the quote immediately afterwards, so taking the
    # first reports 0 for an order that went out correctly priced -- and which
    # one comes first varies between runs of the same data.
    seen = []
    for s in (flow.get("timeline") or []):
        url = str(s.get("url") or "").lower()
        if "items" not in url:
            continue
        body = str(s.get("body") or s.get("resp") or "")
        for m in re.finditer(r'"price"\s*:\s*([0-9]+(?:\.[0-9]+)?)', body):
            v = _num(m.group(1))
            if v is not None:
                seen.append(v)
    if not seen:
        return None
    priced = [v for v in seen if v]
    # The price the order was placed at: the last one the cart actually held.
    # Only when nothing ever priced is 0 the honest answer.
    return priced[-1] if priced else seen[-1]


def _ordered_sku(flow: dict):
    cs = flow.get("checkout_state") or {}
    for q in (cs.get("quote_trace") or []):
        m = re.search(r"sku=([^\s]+)", str(q.get("note") or ""))
        if m:
            return m.group(1)
    # Prefer the sku from a SUCCESSFUL add-to-cart response — the product that
    # really landed in the cart. Falling straight through to every request body
    # picks up failed (out-of-stock) attempts and names the wrong product.
    for s in (flow.get("timeline") or []):
        if not s.get("ok") or "items" not in str(s.get("url") or "").lower():
            continue
        m = re.search(r'"sku"\s*:\s*"([^"]+)"', str(s.get("body") or ""))
        if m:
            return m.group(1)
    for s in (flow.get("timeline") or []):
        m = re.search(r'"sku"\s*:\s*"([^"]+)"', str(s.get("req") or ""))
        if m:
            return m.group(1)
    return None


_PRICE_HINT = re.compile(
    r'("price"\s*:|product-schema-price|__initial_state__|window\.product|'
    r'data-price-amount|price_range|final_price)', re.I)


def _price_source(discovery: dict, flow: dict) -> dict:
    """WHERE the price originates (Cases A/B/C/D from the design):
    A/B) embedded in an HTML/JS blob in the recording; C) a client-invoked pricing
    API; D) client-calculated / not present in the recording at all."""
    for s in (discovery.get("flow") or []):
        b = s.get("body")
        bs = b if isinstance(b, str) else (str(b) if b else "")
        if _PRICE_HINT.search(bs or ""):
            return {"type": "html/js embedded", "location": (s.get("path") or "")[:120]}
    for s in (flow.get("timeline") or []):
        p = str(s.get("url") or s.get("name") or "").lower()
        # "getitby*" endpoints are DELIVERY-DATE services (lead time / ship date):
        # their responses carry qty, leadTime and ship dates, never a price. They
        # were matched here previously and mislabelled the price origin, so they
        # are deliberately NOT treated as a pricing API.
        if any(k in p for k in ("pricing", "/price", "/product/")):
            return {"type": "pricing api (client-invoked)",
                    "location": (s.get("url") or s.get("name") or "")[:120]}
    return {"type": "client-calculated / not in recording", "location": None}


# The reason text itself can contain brackets ("memory (a prior run ...)"), so
# anchor on the "; cart offered" tail rather than the first closing bracket.
_PAY_USED = re.compile(r"payment method used:\s*'([^']*)'\s*\((.*)\); cart offered")
_PAY_REQ = re.compile(r"requested payment_method='([^']*)'")


def _browser_card_note(analysis: dict) -> str:
    """What the real-browser track (Track B) measured for the CARD step, when it
    ran. The offline method carries the volume and the browser track measures the
    real card journey alongside it — the LoadRunner/NeoLoad hybrid shape. Those
    two facts lived in separate report sections, so a reader saw "netterms" and
    concluded the card was never tested. This joins them."""
    bt = (analysis or {}).get("browser_track") or {}
    summary = bt.get("summary") or {}
    endpoints = bt.get("endpoints") or []
    if not (summary or endpoints):
        return ""
    bits = ["card journey measured by the browser track"]
    gw = summary.get("gateway")
    if gw:
        bits.append("gateway=%s" % gw)
    for e in endpoints:
        if "pay" in str(e.get("name") or "").lower() and e.get("p95"):
            try:
                bits.append("card step p95=%dms" % int(float(e["p95"])))
            except (TypeError, ValueError):
                pass
            break
    ok = summary.get("payment_ok")
    if ok is False:
        bits.append("card step FAILED (%s)"
                    % (summary.get("payment_err") or "reason not recorded"))
    elif ok:
        bits.append("card step OK")
    return "; ".join(bits)


def _payment_dim(flow: dict, analysis: dict | None = None):
    """Which payment method the run ACTUALLY used, next to the one the data asked
    for. A hosted card gateway tokenises the card in a third-party iframe, so it
    cannot be completed at the HTTP layer; LT Metrics substitutes an offline method and
    logs why. Surfacing that here stops a reader of the order (or the report)
    concluding the test paid by card when it did not."""
    used = reason = requested = None
    for s in (flow.get("timeline") or []):
        x = str(s.get("extra") or "")
        m = _PAY_USED.search(x)
        if m and not used:
            used, reason = m.group(1), m.group(2)
        m = _PAY_REQ.search(x)
        if m and not requested:
            requested = m.group(1)
    if not (used or requested):
        return None
    if requested and used and requested != used:
        shown = ("requested %s -> used %s (substituted: %s)"
                 % (requested, used, reason or "not HTTP-replayable"))
    else:
        shown = "%s%s" % (used or requested, (" (%s)" % reason) if reason else "")
    _card = _browser_card_note(analysis)
    if _card:
        shown = "%s | %s" % (shown, _card)
    return _dim("payment_method", PARAMETER if requested else RUNTIME_DERIVED,
                value=shown,
                source={"origin": {"type": ("test-data csv" if requested
                                            else "cart payment-methods response")}},
                consumers=[{"endpoint": "carts/mine/payment-information",
                            "field": "paymentMethod.method"}])


def _dim(name, classification, value=None, api_value=None, browser_value=None,
         business_key=None, source=None, consumers=None, confidence="high",
         mismatch=False):
    return {"name": name, "classification": classification, "value": value,
            "api_value": api_value, "browser_value": browser_value,
            "business_key": business_key, "source": source,
            "consumers": consumers or [], "confidence": confidence, "mismatch": mismatch}


def _browser_price(analysis: dict):
    """The REAL price LT Metrics resolved outside the REST layer — from the HTTP price
    heal (server-rendered HTML) or the browser track's rendered-DOM capture."""
    ph = analysis.get("price_heal") or {}
    if ph.get("resolved") and _num(ph.get("price")):
        return _num(ph.get("price")), ph.get("currency", ""), (ph.get("source") or "price-heal")
    bt = (analysis.get("browser_track") or {}).get("summary") or {}
    for p in (bt.get("prices") or []):
        v = _num(p.get("price"))
        if v:
            cur = next((c for c in ("£", "$", "€") if c in str(p.get("price"))), "")
            return v, cur, "browser-track (rendered DOM)"
    return None, "", None


def discover(discovery: dict, analysis: dict) -> dict | None:
    """Build the business-data dependency model + the Browser-vs-API fidelity
    check. Currently emits the PRICE dependency; extend with product/inventory/
    promotion/tax/shipping/currency by adding more _detect_* builders. Never
    raises; returns None when there's nothing to say."""
    try:
        flow = analysis.get("flow") or {}
        deps, recs = [], []

        # --- PRICE dependency + Browser-vs-API fidelity -----------------------
        api_price = _api_item_price(flow)
        br_price, cur, br_source = _browser_price(analysis)
        sku = _ordered_sku(flow)
        if api_price is not None or br_price is not None:
            # Only compare two prices that belong to the SAME product. The heal
            # records which sku it priced; when that is a DIFFERENT product from
            # the one ordered, the numbers are not comparable and firing a
            # "mismatch" would be meaningless (this is what produced the bogus
            # "$87.8 in the browser vs 0 in the API" report).
            heal_sku = (analysis.get("price_heal") or {}).get("for_sku")
            same_product = not (heal_sku and sku and str(heal_sku) != str(sku))
            mismatch = bool(br_price and same_product
                            and (api_price in (0, None) or api_price == 0.0))
            src = _price_source(discovery, flow)
            _source = {"resolved_from": br_source or "unresolved",
                       "origin": src, "currency": cur, "priced_sku": heal_sku}
            if not same_product:
                _source["note"] = (
                    "browser price is for sku %s but the ordered sku is %s — "
                    "different products, so no price comparison was made"
                    % (heal_sku, sku))
            price_dep = _dim(
                "product_price",
                # price is looked up/computed at runtime — NOT a value to freeze in a
                # CSV (a stale price would be replayed). Client-calculated when the
                # browser derives it (base+promo+tax+currency).
                CLIENT_CALCULATED if mismatch else RUNTIME_DERIVED,
                api_value=api_price, browser_value=br_price,
                business_key={"name": "sku", "value": sku} if sku else None,
                source=_source,
                consumers=[{"endpoint": "carts/mine/items", "field": "price"}],
                confidence=("high" if (br_price and same_product) else "low"),
                mismatch=mismatch)
            deps.append(price_dep)
            if mismatch:
                recs.append({"priority": "P1",
                    "title": "Business data mismatch — price resolved in browser, 0 in API",
                    "detail": ("Product price is %s%s in the browser (%s; origin: %s) "
                               "but the API added it at %s. Price is CLIENT_CALCULATED "
                               "here, not a CSV value — the REST order value reflects "
                               "shipping/tax only. Faithful order value needs the "
                               "browser track or the store's pricing endpoint, not "
                               "HTTP replay." % (cur, br_price, br_source,
                                                 src.get("type"), api_price))})

        # --- other classified business values (the model, not just price) ------
        cs = flow.get("checkout_state") or {}
        if sku:
            deps.append(_dim("product_sku", PARAMETER, value=sku,
                             business_key={"name": "sku", "value": sku},
                             source={"origin": {"type": "test-data csv"}},
                             consumers=[{"endpoint": "carts/mine/items", "field": "sku"}]))
        if cs.get("cart_id"):
            deps.append(_dim("cart_id", CORRELATION, value=cs.get("cart_id"),
                             source={"origin": {"type": "server response (carts/mine)"}},
                             consumers=[{"endpoint": "carts/mine/*", "field": "cart id"}]))
        _oids = flow.get("order_ids") or []
        if _oids:
            deps.append(_dim("order_id", SERVER_GENERATED, value=_oids[0],
                             source={"origin": {"type": "place-order response"}},
                             consumers=[{"endpoint": "reporting", "field": "order id"}]))
        if cur:
            deps.append(_dim("currency", BUSINESS_REFERENCE, value=cur,
                             source={"origin": {"type": "storefront locale"}}))
        # Payment method actually used vs requested. Deliberately NOT flagged as a
        # mismatch: substituting an offline method for a hosted card gateway is an
        # accepted, documented limitation of HTTP replay, not a defect — flagging
        # it would fail the fidelity gate on every run and devalue the real signal.
        _pay = _payment_dim(flow, analysis)
        if _pay:
            deps.append(_pay)

        # --- optional Claude enrichment: structured source + consumers ---------
        _ai = _ai_dependencies(discovery, deps)
        if _ai:
            _byname = {d["name"]: d for d in deps}
            for a in _ai:
                d = _byname.get(a.get("name"))
                _src = d.get("source") if d else None
                if not isinstance(_src, dict):
                    _src = {}
                    if d:
                        d["source"] = _src
                if d and isinstance(a.get("source"), dict) and not (_src.get("origin") or {}).get("location"):
                    _src["ai"] = a.get("source")
                if d and a.get("consumers"):
                    d["consumers"] = (d.get("consumers") or []) + list(a["consumers"])[:5]

        if not deps:
            return None
        gate = validate(deps)
        return {"dependencies": deps, "gate": gate, "recommendations": recs}
    except Exception:
        return None


def _ai_dependencies(discovery: dict, deps: list) -> list | None:
    """Claude enrichment: from the recording, infer the SOURCE (html / js / api,
    with a location/path) and the downstream CONSUMER endpoints for each business
    value — the structured dependency the design calls for. Cached + guarded;
    returns None without an API key. Never raises."""
    try:
        from . import llm
        if not llm.available() or not deps:
            return None
        import json
        flow = (discovery.get("flow") or [])[:60]
        listing = [{"m": s.get("method"), "p": (s.get("path") or "")[:120],
                    "has_body": bool(s.get("body"))} for s in flow]
        names = [d["name"] for d in deps]
        _dk = llm.cache_key("bizdata", json.dumps(listing), json.dumps(names))
        cached = llm.cache_get("bizdata", _dk)
        if cached is not None:
            return cached
        prompt = (
            "From this recorded business transaction, for EACH listed business value "
            "identify where it originates and which request consumes it. Use ONLY the "
            "given request paths; never invent endpoints.\n\n"
            "VALUES: " + json.dumps(names) + "\n"
            "REQUESTS (method, path):\n" + json.dumps(listing) + "\n\n"
            'Return ONLY JSON {"dependencies":[{"name":"<value>",'
            '"source":{"type":"html|js|api|server|csv|calculated","location":"path or dom selector or null"},'
            '"consumers":[{"endpoint":"path","field":"field name"}],'
            '"confidence":"high|medium|low"}]}')
        out = llm.json_call(prompt, max_tokens=700,
                            system="You map business values to their source and "
                                   "consumers for load-test fidelity. Strict JSON only.") or {}
        res = out.get("dependencies") if isinstance(out, dict) else None
        res = res if isinstance(res, list) else []
        llm.cache_put("bizdata", _dk, res)
        return res
    except Exception:
        return None


def validate(dependencies: list) -> dict:
    """Business-data gate: a critical value that the browser resolved but the API
    replay didn't (or got 0) is an UNRESOLVED dependency — the run isn't a faithful
    reproduction of the business transaction even if every HTTP call returned 200."""
    unresolved = [d["name"] for d in (dependencies or []) if d.get("mismatch")]
    return {
        "ok": not unresolved,
        "unresolved": unresolved,
        "reason": ("" if not unresolved else
                   "Business-critical value(s) resolved in the browser but missing/0 "
                   "in the API replay: " + ", ".join(unresolved)),
    }
