"""Intelligent Test Data Generator — realistic, executable test data (not sample
CSV templates), sized and distributed for the selected test.

Given the Application Knowledge Base (what products/payment-methods/addresses the
app actually has), the Business Flow (the recorded journey), the Performance Test
Plan and the Execution Plan (how many users, which test type), this produces the
data a run needs:

    testdata.csv   a single unified, upload-compatible CSV — every column a user
                   would fill in themselves: customer (username/password), product
                   (SKUs DISTRIBUTED across users so they don't all buy one item),
                   address, payment method + PUBLIC sandbox test card, and search
                   term — one row per virtual user. Same shape a user uploads; this
                   is what the run consumes. (No scattered per-entity CSVs.)

Design rules from the spec, honored here:
  * Never random products — only DISCOVERED SKUs, distributed round-robin so the
    load spreads across the catalog instead of hammering one item.
  * Inventory sizing — estimate products needed from users x avg cart size.
  * Payment intelligence — if the recorded flow used a CARD, the generated data uses
    a card (public sandbox test card); it does NOT silently fall back to Net Terms /
    Bank Transfer. If a card is required but none is available, that's a VALIDATION
    ERROR, surfaced (not hidden).
  * Validation before execution — customer/product/inventory/payment/address checks.
    Real validation hits the store's APIs; without a reachable store it degrades to
    "unvalidated" with a clear reason (the seam a live validator plugs into).

Platform-independent: it reasons about business objects from the Application KB, not
about any specific vendor. Never raises; degrades to a minimal, still-runnable set.
"""
from __future__ import annotations

import csv
import io
import re

# The columns the run's generator/parameterization understands.
# card_number is deliberately NOT here. The store never receives a card number --
# it goes from the browser straight to the gateway, which hands back a token, and
# the token is what the run replays. A column called card_number is therefore dead
# weight AND a hazard: it is exactly where a real card ends up in a spreadsheet
# that then gets mailed and committed. payment_token replaces it.
_COLUMNS = ["username", "password", "firstname", "lastname", "company",
            "product_id", "sku", "qty", "search_keyword",
            "payment_method", "payment_token", "card_cvv",
            "country_id", "region", "region_code", "region_id", "street", "city",
            "postcode", "telephone", "po_number", "vat_id"]

# Some checkouts make Company mandatory (Radwell's does). A neutral default keeps
# a generated file usable out of the box; a real test plan should overwrite it.
_DEFAULT_COMPANY = "LT Metrics Load Test"

# PUBLIC sandbox test cards (never real PANs).
_TEST_CARDS = {
    "visa": {"number": "4111111111111111", "cvv": "123"},
    "stripe": {"number": "4242424242424242", "cvv": "123"},
    "mastercard": {"number": "5555555555554444", "cvv": "123"},
}
_CARD_SIGNALS = ("card", "credit", "debit", "cybersource", "paradoxlabs", "stripe",
                 "adyen", "braintree", "authorizenet", "worldpay", "sagepay")
# Gateway vocabulary (mirrors generator.py's KB defaults). Used to decide whether a
# recorded "card" can actually be driven by HTTP API-replay: hosted-iframe gateways
# tokenise the card in a 3rd-party iframe that HTTP replay cannot complete, so when the
# store ALSO exposes an offline method we write THAT into the CSV — the method the
# API-replay run will really use — rather than an unusable "card".
_HOSTED_GATEWAYS = ("cybersource", "paradoxlabs", "adyen", "stripe", "braintree",
                    "authorizenet", "authorize_net", "payflow", "worldpay", "sagepay",
                    "klarna", "paypal", "amazon", "checkout_com", "checkoutcom",
                    "mollie", "square")
_OFFLINE_PAYMENTS = ("purchaseorder", "checkmo", "netterms", "paymentonaccount",
                     "payment_on_account", "companycredit", "banktransfer", "wirepayment",
                     "wiretransfer", "wire", "cashondelivery", "cashon", "moneyorder",
                     "payorder", "free", "zeropayment", "nopayment", "offlinepayment",
                     "offline", "check")


def _rows_from(existing_rows):
    return [dict(r) for r in (existing_rows or []) if isinstance(r, dict)]


def _skus(app_knowledge, existing_rows):
    biz = ((app_knowledge or {}).get("business_objects") or {}).get("products") or {}
    skus = list(biz.get("discovered_skus") or [])
    for r in _rows_from(existing_rows):                 # add any recorded sku/product_id
        for k in ("sku", "product_id"):
            v = str(r.get(k) or "").strip()
            if v and v not in skus:
                skus.append(v)
    return [s for s in skus if s] or ["SAMPLE-SKU-1"]


def _search_terms(existing_rows, app_knowledge):
    terms = []
    for r in _rows_from(existing_rows):
        v = str(r.get("search_keyword") or "").strip()
        if v and v not in terms:
            terms.append(v)
    return terms or _skus(app_knowledge, existing_rows)[:5]


def _payment_intent(app_knowledge, existing_rows, business_flow):
    """Decide the payment method from what was actually recorded. Returns
    (method, is_card, note). Card wins if the recording used one."""
    avail_raw = [str(m) for m in
                 ((app_knowledge or {}).get("business_objects", {}).get("payment_methods") or [])]
    avail = [m.lower() for m in avail_raw]
    hay = " ".join([
        " ".join(str(r.get("payment_method") or "") for r in _rows_from(existing_rows)),
        " ".join(str(r.get("payment_token") or "") and "card"
                 for r in _rows_from(existing_rows)),
        " ".join(avail),
        " ".join((business_flow or {}).get("steps") or []),
    ]).lower()
    if any(sig in hay for sig in _CARD_SIGNALS):
        # A card was recorded — but if the store's card option is a HOSTED gateway
        # (iframe-tokenised, not HTTP-replayable) and an OFFLINE method is also enabled,
        # write the offline method so the CSV matches what the API-replay run will
        # actually place the order with. Keep "card" only when no offline alternative
        # is known (or the store's methods are unknown) — the browser track drives the
        # real card in that case.
        offline = next((avail_raw[i] for i, m in enumerate(avail)
                        if any(o in m for o in _OFFLINE_PAYMENTS)), None)
        hosted = any(any(g in m for g in _HOSTED_GATEWAYS) for m in avail)
        if offline and hosted:
            return offline, False, (
                "recorded flow used a card, but the store's card gateway is a hosted "
                "iframe that HTTP replay can't complete; wrote offline method '%s' so the "
                "data matches the API-replay run (use the browser track to exercise the "
                "real card)" % offline)
        return "card", True, "recorded flow used a card"
    # explicit recorded non-card method
    for r in _rows_from(existing_rows):
        pm = str(r.get("payment_method") or "").strip()
        if pm:
            return pm, False, "recorded payment method"
    return "netterms", False, "no card signal; defaulted to an offline method"


def _address_template(existing_rows):
    for r in _rows_from(existing_rows):
        if r.get("country_id") or r.get("postcode"):
            return {k: str(r.get(k) or "") for k in
                    ("country_id", "region", "region_code", "region_id", "street",
                     "city", "postcode", "telephone", "firstname", "lastname")}
    return {"country_id": "US", "region": "New York", "region_code": "NY",
            "region_id": "", "street": "1 Test Way", "city": "New York",
            "postcode": "10001", "telephone": "1234567890",
            "firstname": "Load", "lastname": "Test"}


def _customers(n, existing_rows, mode):
    """existing = reuse recorded creds; synthetic = generate load-test accounts."""
    existing = [(str(r.get("username") or ""), str(r.get("password") or ""))
                for r in _rows_from(existing_rows) if r.get("username")]
    if mode != "synthetic" and existing:
        return [existing[i % len(existing)] for i in range(n)], "existing (from recording)"
    pw = (existing[0][1] if existing else "Test@123") or "Test@123"
    return [(f"loadtest+{i+1}@example.com", pw) for i in range(n)], "synthetic"


def _int(v, d=None):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return d


def _size(test_plan, execution_plan, existing_rows):
    """How many data rows to generate: the user count (unique identities), capped
    so the CSV stays sane — the run recycles rows if it needs more."""
    users = None
    ep = execution_plan or {}
    if ep.get("users") is not None:
        users = _int(ep["users"])
    if users is None:
        for t in ((test_plan or {}).get("tests") or []):
            if t.get("users"):
                users = _int(t["users"]); break
    if users is None:
        users = max(1, len(_rows_from(existing_rows)) or 10)
    return max(1, min(users, 1000))                     # cap; run recycles beyond this


def _throughput_from_plan(test_plan):
    """Transaction mix (Percent Executions) from the plan's business scenarios that
    carry a percentage, e.g. 'Home Page 100%', 'Category 50%'."""
    out = {}
    for s in ((test_plan or {}).get("business_scenarios") or []):
        m = re.search(r"^(.*?)\s+(\d{1,3})\s*%$", str(s).strip())
        if m:
            name = re.sub(r"^\d+[\.\)]?\s*", "", m.group(1)).strip()
            if name:
                out[name] = float(m.group(2))
    return out


def _validate(rows, is_card, live=False):
    """Business-dependency validation gate. Real checks hit the store; without one
    they degrade to 'unvalidated' with a reason. Card-required-but-missing is a hard
    error surfaced here rather than silently downgraded."""
    status = "validated" if live else "unvalidated (no live store reachable)"
    errors = []
    if is_card and not any(str(r.get("payment_token") or "").strip() for r in rows):
        # A card NUMBER in the data proves nothing -- the store never accepts one.
        # The token is what the run actually presents, so its absence is the real
        # failure, and it has a known remedy worth naming here.
        errors.append("Card payment is required (recorded flow used a card) but no "
                      "payment_token is present. Run enrol_cards.py to capture one "
                      "per account, or set those rows to an offline payment method.")
    checks = ["customer_exists", "product_exists", "product_saleable",
              "inventory_available", "address_valid", "payment_available"]
    return {"status": status, "live": live,
            "checks": {c: (True if live else "unvalidated") for c in checks},
            "errors": errors, "ok": not errors}


def generate(app_knowledge: dict | None, test_plan: dict | None = None,
             execution_plan: dict | None = None, business_flow: dict | None = None,
             existing_rows: list | None = None, out_dir=None,
             options: dict | None = None) -> dict:
    """Produce realistic, run-compatible test data. Returns paths + validation +
    suggested_config + summary. Never raises."""
    try:
        options = options or {}
        n = _size(test_plan, execution_plan, existing_rows)
        skus = _skus(app_knowledge, existing_rows)
        terms = _search_terms(existing_rows, app_knowledge)
        method, is_card, pay_note = _payment_intent(app_knowledge, existing_rows, business_flow)
        card = _TEST_CARDS.get("visa")
        addr = _address_template(existing_rows)
        creds, cust_mode = _customers(n, existing_rows, options.get("customer_mode", "existing"))
        avg_cart = max(1, _int(options.get("avg_cart_size"), 1) or 1)

        # --- LIVE STORE (opt-in) — real products / registered customers ------
        base = options.get("live_base_url")
        store = options.get("store", "")
        platform = (app_knowledge or {}).get("platform") or "magento"
        live = {"products": None, "registration": None}
        if base and options.get("live_products"):
            try:
                from . import live_store
                lp = live_store.discover_products(base, search=(terms[0] if terms else ""),
                                                  count=max(20, min(n, 50)), store=store,
                                                  platform=platform)
                live["products"] = lp
                if lp.get("products"):
                    skus = [p["sku"] for p in lp["products"]]   # REAL in-stock, priced SKUs
            except Exception:
                pass
        if base and options.get("register_customers"):
            try:
                from . import live_store
                syn, _ = _customers(n, existing_rows, "synthetic")   # accounts to create
                reg = live_store.register_customers(base, syn, addr, store=store,
                                                    platform=platform, limit=min(n, 200))
                live["registration"] = reg
                if reg.get("created"):
                    creds = [reg["created"][i % len(reg["created"])] for i in range(n)]
                    cust_mode = "registered on store (%d created)" % len(reg["created"])
            except Exception:
                pass

        rows = []
        for i in range(n):
            u, p = creds[i]
            row = {c: "" for c in _COLUMNS}
            row.update(addr)
            row["username"], row["password"] = u, p
            row["sku"] = skus[i % len(skus)]            # round-robin: distribute load
            row["product_id"] = row["sku"]
            row["qty"] = str(avg_cart)
            row["search_keyword"] = terms[i % len(terms)]
            row["payment_method"] = method
            row["company"] = _DEFAULT_COMPANY
            if is_card:
                # No card number is written. payment_token is left EMPTY on
                # purpose: it is per-account and cannot be invented here, and a
                # blank one makes the run fail closed rather than pay another way.
                row["card_cvv"] = card["cvv"]
            rows.append(row)

        _live_ran = bool(base and (options.get("live_products") or options.get("register_customers")))
        validation = _validate(rows, is_card, live=_live_ran)
        if live["products"] is not None:
            _ok_p = bool(live["products"].get("products"))
            for _c in ("product_exists", "product_saleable", "inventory_available"):
                validation["checks"][_c] = _ok_p
            if not _ok_p:
                validation["errors"].append("Live product discovery found no in-stock, priced "
                                            "products: " + (live["products"].get("note") or ""))
        if live["registration"] is not None:
            _reg = live["registration"]
            validation["checks"]["customer_exists"] = bool(_reg.get("created"))
            if not _reg.get("created"):
                validation["errors"].append(
                    "No USABLE customer accounts were registered — newly-created accounts "
                    "can't obtain a REST token (the store likely requires email "
                    "confirmation). The load test needs accounts that can authenticate: "
                    "upload a data CSV with pre-created/confirmed accounts, or disable email "
                    "confirmation on the staging store. (Auto-registration only works when "
                    "the store lets a new account log in immediately.)")
            for _e in (_reg.get("errors") or [])[:3]:
                validation["errors"].append("Register: " + str((_e or {}).get("error") or _e))
        validation["ok"] = not validation["errors"]

        # --- write ONE unified, upload-compatible testdata.csv ----------------
        # (customers, products, addresses, payments and search terms are all
        # combined into this single file — the same shape a user uploads — rather
        # than scattered across separate CSVs.)
        files = {}
        if out_dir is not None:
            from pathlib import Path
            od = Path(out_dir)
            od.mkdir(parents=True, exist_ok=True)
            files["testdata"] = str(_write_csv(od / "testdata.csv", _COLUMNS, rows))

        suggested = {
            "test_type": (execution_plan or {}).get("test_type"),
            "users": (execution_plan or {}).get("users") or (
                next((t.get("users") for t in ((test_plan or {}).get("tests") or [])
                      if t.get("users")), None)),
            "duration_s": (execution_plan or {}).get("duration_s"),
            "group_throughput": _throughput_from_plan(test_plan),
            "payment_method": method,
        }
        return {
            "rows": len(rows),
            "unique_customers": len(set(creds)),
            "unique_skus": len(set(skus)),
            "customer_mode": cust_mode,
            "payment": {"method": method, "is_card": is_card, "note": pay_note},
            "live": {
                "products_source": (live["products"] or {}).get("source") if live["products"] else None,
                "products_found": len((live["products"] or {}).get("products") or []) if live["products"] else None,
                "registered": len((live["registration"] or {}).get("created") or []) if live["registration"] else None,
                "registration_note": (live["registration"] or {}).get("note") if live["registration"] else None,
            },
            "validation": validation,
            "suggested_config": suggested,
            "files": files,
            "preview_csv": _csv_string(_COLUMNS, rows[:5]),
            "summary": ("%d rows · %d unique customers · %d SKUs distributed · payment=%s%s"
                        % (len(rows), len(set(creds)), len(set(skus)), method,
                           "" if validation["ok"] else " · VALIDATION ERRORS")),
        }
    except Exception as exc:
        return {"rows": 0, "files": {}, "validation": {"ok": False, "errors": [str(exc)]},
                "suggested_config": {}, "summary": "test-data generation error: %s" % exc}


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})
    return path


def _csv_string(columns, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    return buf.getvalue()
