"""Parameterization & Correlation agent.

Given a parsed recording flow, this agent:
  * finds DYNAMIC values that must be CORRELATED (captured from one response and
    reused in later requests — form_key, tokens, quote/cart/order ids, UUIDs), and
  * finds INPUT fields that should be PARAMETERIZED from a CSV (credentials, card,
    shipping/billing address, contact, search/product, quantity, coupon), grouped
    into human-friendly buckets, and
  * emits a sample CSV template (headers + one example row) for the user to fill.

Everything is derived from the RECORDING — no assumption of a search→PDP→cart→
checkout journey. Deterministic core; optional AI enrichment when a key is set.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl

# Field-name -> (group, csv_column). Matching is case-insensitive substring on
# the request field/key name. Order matters: first match wins.
#
# NOTE: this map no longer carries opinionated placeholder values. The sample
# value for every column is now taken from what was ACTUALLY recorded (the HTTP
# body value or the Selenium-typed value) — see `analyze()`'s value-resolution
# pass. The 4th tuple element is kept only for backward-compatible unpacking and
# is always "" so no site/platform-flavoured default can leak into a sample CSV.
_FIELD_MAP = [
    # credentials
    (r"(login\[username\]|^username$|^email$|customer_email|email_address)",
     "Credentials", "username", ""),
    (r"(login\[password\]|^password$|passwd|pwd)",
     "Credentials", "password", ""),
    # card / payment
    (r"(card_?number|cardnumber|cc_?number|pan)", "Card", "card_number", ""),
    (r"(card_?expiry_?month|expmonth|cc_?exp_?month|expiry_month)",
     "Card", "card_expiry_month", ""),
    (r"(card_?expiry_?year|expyear|cc_?exp_?year|expiry_year)",
     "Card", "card_expiry_year", ""),
    (r"(card_?cvn|cvv|cvc|card_?code|securitycode)", "Card", "card_cvv", ""),
    (r"(card_?holder|name_?on_?card|cardholder)", "Card", "card_holder", ""),
    (r"(payment_?token|stored_?card|card_?id|token_?id)",
     "Card", "payment_token", ""),
    # payment method selection (e.g. paymentMethod.method = paradoxlabs_cybersource)
    (r"(^method$|payment_?method|paymentmethod)", "Payment", "payment_method", ""),
    # A price agreement chosen on the product page. B2B stores post the whole
    # selection back -- id, price group, title and price -- and reject an add
    # whose contract is no longer valid for the account. Recorded values go
    # stale, so they are data rather than script.
    (r"(^contract_?id$|contractid)", "Contract", "contract_id", ""),
    (r"(^price_?group_?id$|pricegroupid)", "Contract", "price_group_id", ""),
    (r"(^contract_?title$|contracttitle)", "Contract", "contract_title", ""),
    (r"(^contract_?price$|contractprice)", "Contract", "contract_price", ""),
    (r"(shipping_?method_?code|method_?code)", "Payment", "shipping_method_code", ""),
    (r"(shipping_?carrier_?code|carrier_?code)", "Payment", "shipping_carrier_code", ""),
    # shipping / billing address
    (r"(shipping.*first|first_?name|firstname)", "Address", "firstname", ""),
    (r"(shipping.*last|last_?name|lastname)", "Address", "lastname", ""),
    (r"(street|address_?line|addressline|addr1)", "Address", "street", ""),
    (r"(\bcity\b|city\]|town)", "Address", "city", ""),
    (r"(post_?code|postcode|zip_?code|zip|postal)", "Address", "postcode", ""),
    # region MUST be split: regionId is a NUMERIC id, regionCode is a short code,
    # region is the display name. Conflating them injects a name into the int id
    # field and the server rejects it ("int type was expected"). Ordered so the
    # id/code patterns win before the generic name pattern.
    (r"(region_?id|regionid)", "Address", "region_id", ""),
    (r"(region_?code|regioncode)", "Address", "region_code", ""),
    (r"(\bregion\b|state|province|county)", "Address", "region", ""),
    (r"(country_?id|countrycode|country)", "Address", "country_id", ""),
    (r"(telephone|phone|mobile|contact_?number)", "Address", "telephone", ""),
    (r"(company|organis|organiz)", "Address", "company", ""),
    (r"(vat|tax_?id)", "Address", "vat_id", ""),
    # search / catalog
    (r"(^q$|search|keyword|query|search_?term)", "Search", "search_keyword", ""),
    (r"(^product$|sku|product_?id|productid|item_?id|ndc)", "Search", "product_id", ""),
    (r"(^qty$|quantity)", "Search", "qty", ""),
    (r"(coupon|promo|voucher|discount_?code)", "Search", "coupon", ""),
    (r"(po_?number|purchase_?order|ponumber)", "Order", "po_number", ""),
]

# Cells in the sample CSV that the user MUST fill carry this visible sentinel.
# Validation treats a sentinel value as empty, so a downloaded-but-unfilled
# sample stays BLOCKED for those fields until the user replaces them.
_FILL_SENTINEL = "<<FILL:"


def _fill(col: str, hint: str = "") -> str:
    return "<<FILL: %s%s>>" % (col, (" — " + hint) if hint else "")


def _is_fill_placeholder(v) -> bool:
    return str(v or "").strip().startswith(_FILL_SENTINEL)


# Characters that belong to the syntax carrying a value, not to the value. A
# GraphQL body reached a sample CSV as the search keyword because nothing here
# asked the question. Parentheses are absent on purpose: a recorded telephone
# number is "+1 (354) 643-6356".
_NOT_A_VALUE = set('{}[]<>"\\')


def _looks_like_a_value(s: str) -> bool:
    """Is this something a person would type into a field, or a piece of the
    request it was captured from?"""
    if not s:
        return False
    if any(c in _NOT_A_VALUE for c in s):
        return False
    return "  " not in s          # runs of whitespace mean formatted source


def _clean_sample(v) -> str:
    """A recorded value trimmed to a CSV-friendly single-line sample.

    Returns "" for anything that is not a value: an empty cell is already how
    this tool says the recording did not supply one, and a required column then
    shows its <<FILL: ...>> marker rather than a plausible-looking blob.
    """
    s = "" if v is None else str(v).replace("\r", " ").replace("\n", " ").strip()
    return s[:80] if _looks_like_a_value(s) else ""

# Values that are DYNAMIC (server-issued) and must be correlated, not parameterized.
_CORRELATION_KEYS = [
    ("form_key", r"form[_-]?key"),
    ("csrf_token", r"csrf|authenticity_token|_token|csrfmiddlewaretoken"),
    ("customer_token", r"bearer|access[_-]?token|customer[_-]?token"),
    ("quote_id", r"quote[_-]?id|cart[_-]?id|masked[_-]?id"),
    ("order_id", r"order[_-]?id|increment[_-]?id|entity[_-]?id"),
    ("uenc", r"uenc"),
    ("address_id", r"address[_-]?id|customeraddressid"),
]

# how LT Metrics already handles each correlation at runtime (shown to the user)
_CORRELATION_HANDLING = {
    "form_key": "captured fresh from a warm-up GET and re-injected on every POST",
    "csrf_token": "extracted from the form/page and re-injected on submit",
    "customer_token": "obtained via the REST customer-token call and sent as Bearer",
    "quote_id": "uses the logged-in customer's own cart (carts/mine)",
    "order_id": "extracted from the place-order response for reporting",
    "uenc": "recomputed per request; recorded value is dropped",
    "address_id": "replaced with the logged-in customer's default address id",
}


def _iter_fields(flow):
    """Yield (field_name, sample_value) from every request body in the flow."""
    for s in flow or []:
        body = s.get("body")
        if isinstance(body, dict):
            for k, v in body.items():
                yield str(k), v
        elif isinstance(body, str) and body:
            b = body.strip()
            if b.startswith("{"):
                try:
                    obj = json.loads(b)
                    yield from _iter_json(obj)
                    continue
                except Exception:
                    pass
            # form-encoded
            for k, v in parse_qsl(b):
                yield k, v


def _iter_json(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                yield from _iter_json(v, k)
            elif isinstance(v, list):
                # list of scalars -> yield each under this key (e.g. street[]);
                # list of objects -> recurse into them
                for it in v:
                    if isinstance(it, (dict, list)):
                        yield from _iter_json(it, k)
                    else:
                        yield str(k), it
            else:
                yield str(k), v
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_json(it, prefix)


def analyze(flow: list, selenium_inputs: list | None = None,
            ui_steps: list | None = None) -> dict:
    """Return {correlations, groups, columns, sample_csv, notes}.

    `selenium_inputs` = [{field, target, secret, sample}, ...] extracted from a
    combined JMeter+Selenium recording, where credentials/search are typed via UI
    actions (not HTTP bodies). Without this, such recordings produce a sample CSV
    with no username/password columns.

    `ui_steps` = [{group, verb, target, value, step_label}, ...] — EVERY recorded
    browser action (`recording.py`'s `_extract_selenium_steps`), not just the 3
    categories `selenium_inputs` classifies. Any `type*` action with a target and
    a value is folded in as a candidate field under its raw target name (e.g. a
    typed "shipping-postcode" reaches the same Address/Card/Payment `_FIELD_MAP`
    matching as a submitted body field would). Each resulting field is tagged
    with `evidence` — "typed" (a UI type-action) or "submitted" (a POST/PUT
    body) — so a report can show WHY a field was flagged, not just that it was."""
    flow = flow or []
    field_names = {}
    evidence = {}
    for name, val in _iter_fields(flow):
        field_names.setdefault(name, val)
        evidence.setdefault(name, "submitted")
    # fold Selenium-typed inputs in as if they were recorded fields
    _sel_samples = {}
    for si in (selenium_inputs or []):
        fld = si.get("field")
        if fld:
            field_names.setdefault(fld, si.get("sample", ""))
            evidence.setdefault(fld, "typed")
            if si.get("sample"):        # recorded value -> pre-fill the sample CSV
                _sel_samples[fld] = si["sample"]
    # fold in EVERY typed browser action (not just the 3 hardcoded categories
    # above) so address/card/payment fields entered only via the UI are seen too
    for step in (ui_steps or []):
        verb = str(step.get("verb") or "").lower()
        target = step.get("target")
        value = step.get("value")
        if not (verb.startswith("type") and target and value):
            continue
        field_names.setdefault(target, value)
        evidence.setdefault(target, "typed")
        _sel_samples.setdefault(target, value)

    # --- correlations (dynamic values) ---
    correlations = []
    seen_corr = set()
    for name in field_names:
        low = name.lower()
        for corr_name, pat in _CORRELATION_KEYS:
            if re.search(pat, low) and corr_name not in seen_corr:
                seen_corr.add(corr_name)
                correlations.append({
                    "value": corr_name,
                    "field": name,
                    "handling": _CORRELATION_HANDLING.get(corr_name, "correlated at runtime"),
                })

    # --- parameterizable input fields, grouped ---
    columns, groups = [], {}
    for name in field_names:
        low = name.lower()
        # skip correlation fields — those are handled automatically
        if any(re.search(pat, low) for _, pat in _CORRELATION_KEYS):
            continue
        # A field carrying a request body is not a field a value can be written
        # into. GraphQL puts its whole document in one named "query", which
        # matches the search pattern; binding search_keyword there replaced the
        # query with the keyword and the call came back 400. Judged on the
        # recorded VALUE, so a field the recording left empty still binds --
        # that is the case the <<FILL: ...>> marker exists for.
        _recorded = field_names.get(name)
        if _recorded not in (None, "") and not _clean_sample(_recorded):
            continue
        for pat, group, col, sample in _FIELD_MAP:
            if re.search(pat, low):
                if col not in columns:
                    columns.append(col)
                    groups.setdefault(group, []).append({
                        "column": col, "from_field": name, "sample": sample,
                        "evidence": evidence.get(name, "submitted")})
                break

    group_list = [{"group": g, "fields": f,
                   "why": _GROUP_WHY.get(g, "values to drive the recorded requests")}
                  for g, f in groups.items()]

    # ---- Real-time AI pass -------------------------------------------------
    # Claude reads the ACTUAL recorded fields and identifies correlations +
    # parameterization, catching anything the heuristics miss (unusual field
    # names, nested payment/address structures, custom platforms). Merged OVER
    # the deterministic baseline; degrades cleanly without an API key.
    notes, ai = None, _ai_fields(flow)
    if ai:
        notes = ai.get("notes")
        cvals = {c["value"] for c in correlations}
        for c in ai.get("correlations", []):
            if c["value"] not in cvals:
                correlations.append(c)
                cvals.add(c["value"])
        existing_ff = {f["from_field"] for g in group_list for f in g.get("fields", [])}
        gmap = {g["group"]: g for g in group_list}
        for g in ai.get("groups", []):
            tgt = gmap.get(g["group"])
            if tgt is None:
                tgt = {"group": g["group"], "fields": [],
                       "why": g.get("why") or _GROUP_WHY.get(g["group"],
                              "values for the recorded requests")}
                gmap[g["group"]] = tgt
                group_list.append(tgt)
            for f in g.get("fields", []):
                if f["from_field"] in existing_ff:
                    continue
                existing_ff.add(f["from_field"])
                f.setdefault("evidence", evidence.get(f["from_field"], "ai-detected"))
                tgt["fields"].append(f)
                if f["column"] not in columns:
                    columns.append(f["column"])

    # --- resolve each column's sample from what was ACTUALLY recorded ---------
    # Priority: the real value captured from the recording (an HTTP body value OR
    # a Selenium-typed value — both live in `field_names`, keyed by the recorded
    # field name) > an AI-provided recorded example > nothing. No opinionated
    # hardcoded defaults are ever used; unknown-but-required cells are flagged
    # with a <<FILL: …>> sentinel by `_sample_csv` so the user can see and fill
    # exactly what the recording didn't supply.
    value_by_col, source_by_col = {}, {}
    for g in group_list:
        for f in g.get("fields", []):
            col = f["column"]
            real = _clean_sample(field_names.get(f.get("from_field")))
            if not real and f.get("sample") and not _is_fill_placeholder(f.get("sample")):
                real = _clean_sample(f["sample"])      # AI-provided recorded example
            if real and col not in value_by_col:
                value_by_col[col] = real
                source_by_col[col] = "recorded"

    # --- product-identifier columns must ALWAYS be offered -------------------
    # A load test always needs something to put in the cart, so `search_keyword`
    # and `product_id` are guaranteed in the sample CSV even if the recording
    # surfaced the value under an unmatched field name. (Fixes a regression where
    # the search term, typed via a UI field the field-map didn't classify, dropped
    # the search_keyword column entirely.)
    for _pc in ("search_keyword", "product_id"):
        if _pc not in columns:
            columns.append(_pc)
            _psg = next((g for g in group_list if g["group"] == "Search"), None)
            if _psg is None:
                _psg = {"group": "Search", "fields": [], "why": _GROUP_WHY["Search"]}
                group_list.append(_psg)
            _psg["fields"].append({"column": _pc, "from_field": _pc, "sample": "",
                                   "evidence": "optional"})

    # --- explicit purchasable SKU column (decided from the recording) ----------
    # Always offer a `sku` column so users can add the exact in-stock, priced,
    # purchasable variant directly. Mark it REQUIRED only when the recording shows
    # a configurable product (search/id alone can't add it).
    sku_required = _needs_sku(flow)
    _rec_sku = _recorded_sku(flow)
    # If the recording added an EXPLICIT sku that isn't the search term / product id,
    # the replay can't re-derive it from search -> the sku column is required.
    if _rec_sku:
        _search_vals = {str(value_by_col.get(c, "")).strip()
                        for c in ("search_keyword", "product_id")}
        if _rec_sku not in _search_vals:
            sku_required = True
    if "sku" not in columns:
        columns.append("sku")
        _sg = next((g for g in group_list if g["group"] == "Search"), None)
        if _sg is None:
            _sg = {"group": "Search", "fields": [], "why": _GROUP_WHY["Search"]}
            group_list.append(_sg)
        _sg["fields"].append({"column": "sku", "from_field": "sku", "sample": "",
                              "evidence": "recorded" if sku_required else "optional"})
    if _rec_sku and not value_by_col.get("sku"):
        value_by_col["sku"] = _rec_sku
        source_by_col["sku"] = "recorded"

    # --- columns checkout needs that a recording may never reveal -------------
    # A recording only yields a column when its value crossed the wire. Two never
    # do, and both stop a run cold if they are absent from the file:
    #
    #   company        checkouts that ask for a business name mark it REQUIRED,
    #                  and a saved address means it is never typed during a
    #                  recording, so it is invisible to parameterisation.
    #   payment_token  the store's stand-in for the card. It is minted per
    #                  ACCOUNT at checkout, so one recording can only ever show
    #                  one -- and a pool needs one each.
    #
    # Offer both, the same way `sku` is offered above: present, empty, optional.
    # Empty is correct here. company has a runtime default; payment_token cannot
    # be invented for an account and a blank one makes the run fail closed rather
    # than quietly pay another way. enrol_cards.py fills it.
    for _extra, _grp, _why in (("company", "Address", "business name, required by some checkouts"),
                               ("payment_token", "Card", "stored-card token, one per account")):
        if _extra in columns:
            continue
        columns.append(_extra)
        _xg = next((g for g in group_list if g["group"] == _grp), None)
        if _xg is None:
            _xg = {"group": _grp, "fields": [],
                   "why": _GROUP_WHY.get(_grp, _why)}
            group_list.append(_xg)
        _xg["fields"].append({"column": _extra, "from_field": _extra, "sample": "",
                              "evidence": "optional"})

    # annotate each field so the UI/report can highlight required vs optional and
    # show WHERE the sample came from (recorded vs still needs input)
    required_cols = set(_required_columns(columns))
    if sku_required:
        required_cols.add("sku")          # configurable product -> sku is blocking
    for g in group_list:
        for f in g.get("fields", []):
            col = f["column"]
            f["sample"] = value_by_col.get(col, "")
            f["required"] = (col in required_cols) or (col in _PRODUCT_COLUMNS)
            f["source"] = source_by_col.get(col, "needs input")

    return {
        "correlations": correlations,
        "groups": group_list,
        "columns": columns,
        "required_columns": sorted(required_cols),
        "product_columns": [c for c in columns if c in _PRODUCT_COLUMNS],
        "optional_columns": [c for c in columns if c in _OPTIONAL_COLUMNS],
        "sample_csv": _sample_csv(columns, value_by_col, required_cols),
        "uploads_needed": [g["group"] for g in group_list],
        "notes": notes,
        "ai_used": bool(ai),
        "ui_steps_used": len(ui_steps or []),
    }


def _required_columns(columns: list) -> list:
    """Columns that BLOCK a run if empty — everything LT Metrics can't auto-handle and
    that isn't a product identifier (product columns need 'at least one', handled
    separately)."""
    return [c for c in (columns or [])
            if c not in _OPTIONAL_COLUMNS and c not in _PRODUCT_COLUMNS]


_GROUP_WHY = {
    "Contract": "The price agreement this account buys under. B2B stores post "
                "the whole selection with the add-to-cart and refuse one that "
                "is no longer valid, so a recording's contract goes stale — "
                "change it here rather than recording the journey again.",
    "Address": "delivery and billing details typed at checkout",
    "Credentials": "log in as different users (one account per concurrent user)",
    "Card": "supply test-mode card / stored-token details at payment",
    "Payment": "payment method + shipping method/carrier selected at checkout",
    "Address": "shipping & billing address values used at checkout",
    "Search": "search terms / product ids / quantities to vary the catalog load",
    "Order": "purchase-order / quote reference values",
}


def _sample_csv(columns: list, value_by_col: dict | None = None,
                required_cols=None) -> str:
    """Build the sample CSV: one header row + one example row. Every cell is
    filled from the RECORDING where possible; cells the recording didn't supply
    are marked <<FILL: col — required>> when required (so the user sees exactly
    what to complete) and left blank when optional (LT Metrics fills them at runtime).
    Written with the csv module so recorded values containing commas/quotes are
    escaped correctly."""
    if not columns:
        return ""
    import csv as _csv
    import io
    value_by_col = value_by_col or {}
    required_cols = set(required_cols or ())
    product_has_value = any(value_by_col.get(c) for c in columns
                            if c in _PRODUCT_COLUMNS)
    buf = io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    row = []
    for c in columns:
        v = value_by_col.get(c, "")
        if v:
            row.append(v)                                   # real recorded value
        elif c in required_cols:
            row.append(_fill(c, "required"))                # must be filled in
        elif c in _PRODUCT_COLUMNS and not product_has_value:
            row.append(_fill(c, "fill at least one product column"))
        else:
            row.append("")                                  # optional — auto-filled
    w.writerow(row)
    return buf.getvalue()


# Columns LT Metrics can handle at RUNTIME without a CSV value (address resolved from
# the customer profile; payment auto-selected from the live methods; card via
# gateway test-mode). Empty here is a WARNING, never a blocker.
_OPTIONAL_COLUMNS = {
    "region_id", "region_code", "region", "country_id", "telephone", "company",
    "vat_id", "firstname", "lastname", "street", "city", "postcode",
    "payment_method", "shipping_method_code", "shipping_carrier_code",
    "card_number", "card_expiry_month", "card_expiry_year", "card_cvv",
    "card_holder", "payment_token", "coupon", "po_number", "qty",
    # `sku` is optional by DEFAULT (search/product_id resolves a simple product),
    # but _needs_sku() promotes it to REQUIRED (blocking) when the recording shows
    # a configurable product that can't be added without an explicit child SKU.
    "sku",
}
# At least ONE of these must carry a value (something to put in the cart).
_PRODUCT_COLUMNS = {"search_keyword", "product_id"}


def _needs_sku(flow) -> bool:
    """Decide from the RECORDING whether an explicit purchasable SKU is REQUIRED.
    True when a cart-add shows a CONFIGURABLE product (needs options) or the
    recording carried an explicit sku that a plain search/id can't reproduce —
    e.g. Radwell condition variants, where search resolves to the parent and
    Magento answers 'You need to choose options' or picks an out-of-stock child."""
    for s in (flow or []):
        b = s.get("body")
        bs = b if isinstance(b, str) else (str(b) if isinstance(b, (dict, list)) else "")
        low = (bs or "").lower()
        if any(k in low for k in ("configurable_item_options", "product_option",
                                  "super_attribute")):
            return True
    return False


def _recorded_sku(flow):
    """The exact sku the recording added to cart, if one was captured — used as the
    sample value so the user sees what a valid purchasable SKU looks like."""
    for s in (flow or []):
        b = s.get("body")
        bs = b if isinstance(b, str) else (str(b) if isinstance(b, (dict, list)) else "")
        m = re.search(r'["\']sku["\']\s*:\s*["\']([^"\']+)["\']', bs or "")
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def validate(columns_required: list, rows: list) -> dict:
    """Validate an uploaded CSV against the required columns, separating BLOCKING
    issues (missing/empty required values that would break the run) from optional
    fields LT Metrics auto-handles. Returns {ok, blocking, missing_columns,
    empty_columns, warnings, row_count, messages}. `ok` gates test generation."""
    required = list(columns_required or [])
    present = set()
    for r in (rows or []):
        present.update(k for k in r.keys() if k)

    def _filled(r, c):
        """A cell counts as filled only if it has a value that ISN'T a leftover
        <<FILL: …>> sample placeholder."""
        v = str((r.get(c) or "")).strip()
        return bool(v) and not _is_fill_placeholder(v)

    def _empty(c):
        return (not rows) or all(not _filled(r, c) for r in rows)

    # required cells still carrying the downloaded <<FILL: …>> placeholder
    placeholders = sorted({c for c in required for r in (rows or [])
                           if _is_fill_placeholder(r.get(c))})

    missing, empty, warnings = [], [], []
    for c in required:
        if c in _PRODUCT_COLUMNS:
            continue                       # handled as a group below
        opt = c in _OPTIONAL_COLUMNS
        if c not in present:
            (warnings if opt else missing).append(c)
        elif _empty(c):
            (warnings if opt else empty).append(c)

    # product identifier: need at least one of search_keyword / product_id filled
    prod_req = [c for c in required if c in _PRODUCT_COLUMNS]
    if prod_req and not any((c in present and not _empty(c)) for c in prod_req):
        empty.append(" or ".join(prod_req) + " (need at least one value)")

    messages = []
    if not rows:
        messages.append("The file has no data rows.")
    if missing:
        messages.append("Missing required column(s): " + ", ".join(missing))
    if placeholders:
        messages.append("Replace the <<FILL: …>> sample placeholder(s) with real "
                        "values in: " + ", ".join(placeholders))
    _empty_nonph = [c for c in empty if c not in placeholders]
    if _empty_nonph:
        messages.append("Required value(s) empty: " + ", ".join(_empty_nonph))
    if warnings:
        messages.append("Optional — LT Metrics fills these at runtime, leaving blank is fine: "
                        + ", ".join(warnings))
    blocking = bool(missing or empty) or not rows
    ok = not blocking
    if ok and not warnings:
        messages.append("Looks good — %d row(s), all required values present." % len(rows))
    elif ok:
        messages.append("Ready — %d row(s); required values present." % len(rows))
    return {"ok": ok, "blocking": blocking, "missing_columns": missing,
            "empty_columns": empty, "warnings": warnings,
            "row_count": len(rows or []), "messages": messages}


# Deterministic seed extractors for common e-commerce dynamics. Each rule:
#   name    -> variable name
#   extract -> list of regexes (one capture group) tried against every response
#   inject  -> request field / URL-param / path names to overwrite with the value
_CORR_SEEDS = [
    {"name": "form_key",
     "extract": [r'name="form_key"[^>]*value="([^"]+)"', r'"form_key"\s*:\s*"([^"]+)"',
                 r'form_key=([A-Za-z0-9]+)'],
     "inject": ["form_key"]},
    {"name": "uenc",
     "extract": [r'/uenc/([^/"\\]+)'], "inject": ["uenc"]},
    {"name": "quote_id",
     "extract": [r'"quote_id"\s*:\s*"?(\d+)', r'"entity_id"\s*:\s*"?(\d+)'],
     "inject": ["quoteId", "quote_id", "cartId", "masked_id"]},
    {"name": "order_id",
     "extract": [r'"order_id"\s*:\s*"?(\d+)', r'"increment_id"\s*:\s*"?([A-Za-z0-9]+)'],
     "inject": ["order_id", "orderId"]},
]


def correlation_rules(analysis: dict | None = None) -> list:
    """Build the runtime correlation ruleset: deterministic seeds merged with the
    dynamic values the analyzer (AI + heuristics) identified. Each rule captures a
    value from responses via regex and re-injects it into later requests."""
    rules = {}
    for r in _CORR_SEEDS:
        rules[r["name"]] = {"name": r["name"], "extract": list(r["extract"]),
                            "inject": list(r["inject"])}
    for c in (analysis or {}).get("correlations", []) or []:
        name = re.sub(r"[^a-z0-9_]+", "_", str(c.get("value") or c.get("field") or "").lower()).strip("_")
        if not name:
            continue
        r = rules.setdefault(name, {"name": name, "extract": [], "inject": []})
        for pat in (c.get("extract") or []):
            if pat and pat not in r["extract"]:
                r["extract"].append(str(pat))
        inj = list(c.get("inject") or [])
        if c.get("field"):
            inj.append(c["field"])
        for f in inj:
            if f and f not in r["inject"]:
                r["inject"].append(str(f))
        # synthesize a reasonable extractor from the field name if AI gave none
        if not r["extract"] and c.get("field"):
            fld = re.escape(str(c["field"]))
            r["extract"] = [r'"' + fld + r'"\s*:\s*"?([A-Za-z0-9_\-]+)',
                            fld + r'=([A-Za-z0-9_\-]+)',
                            r'name="' + fld + r'"[^>]*value="([^"]+)"']
    # cap regexes per rule to keep the generated script tidy
    for r in rules.values():
        r["extract"] = r["extract"][:6]
        r["inject"] = r["inject"][:10]
    return list(rules.values())


def _short_val(v) -> str:
    s = "" if v is None else str(v)
    return s[:48]


def _field_inventory(flow, limit: int = 180) -> list:
    """Distinct request field names with an example value + a request path.
    This compact inventory is what the AI reasons over."""
    seen, order = {}, []
    for s in flow or []:
        path = (s.get("path") or "")[:80]
        for name, val in _iter_fields([s]):
            if name and name not in seen:
                seen[name] = {"field": name, "sample": _short_val(val), "path": path}
                order.append(name)
                if len(order) >= limit:
                    return [seen[n] for n in order]
    return [seen[n] for n in order]


def _ai_fields(flow):
    """Real-time AI analysis of the recording. Returns {notes, correlations,
    groups} using the EXACT recorded field names, or None without an API key."""
    try:
        from . import llm
        if not llm.available():
            return None
        inv = _field_inventory(flow)
        if not inv:
            return None
        # cross-process disk cache: the CLI regenerates the script several times
        # per run (orchestrator/heal), and this ~2500-token call is the biggest
        # consumer. Cache the RESULT keyed by the field inventory so repeated
        # generations (and repeated runs of the same recording) skip the call.
        _dk = llm.cache_key("aifields", json.dumps(inv, sort_keys=True))
        _disk = llm.cache_get("aifields", _dk)
        if _disk is not None:
            return _disk
        prompt = (
            "You are configuring a performance test from a recording. Below are the "
            "request FIELDS captured as JSON (field name, example value, a request "
            "path).\n\nIdentify two things:\n"
            "1) CORRELATIONS — dynamic, server-issued values that must be captured "
            "from an earlier response and reused (CSRF / form_key, bearer or customer "
            "tokens, cart / quote / order ids, uenc, nonces, session ids).\n"
            "2) PARAMETERIZATION — INPUT values that should come from a test-data CSV, "
            "grouped by meaning (Credentials, Card, Payment, Address, Search, Order, "
            "or your own group name). Give the EXACT recorded field name for each.\n\n"
            "Return ONLY JSON of the form: {\"notes\":\"2-3 sentences\", "
            "\"correlations\":[{\"value\":\"short_name\",\"field\":\"exact field\","
            "\"handling\":\"how it is correlated\",\"extract\":[\"regex with ONE "
            "capture group to pull this value out of a response body\"],"
            "\"inject\":[\"request field or URL-param names to overwrite with it\"]}], "
            "\"groups\":[{\"group\":\"Name\","
            "\"why\":\"short reason\",\"fields\":[{\"column\":\"snake_case_csv_column\","
            "\"from_field\":\"exact recorded field name\",\"sample\":\"example\"}]}]}.\n"
            "Use the EXACT from_field strings from the list; never invent fields.\n\n"
            "FIELDS:\n" + json.dumps(inv))
        res = llm.json_call(prompt, max_tokens=2500,
                            system="You are a senior performance engineer setting up "
                                   "correlation and parameterization for a load test.")
        if not isinstance(res, dict):
            return None
        corr = []
        for c in res.get("correlations", []) or []:
            if isinstance(c, dict) and c.get("field"):
                ex = c.get("extract")
                ex = [str(x) for x in ex if isinstance(x, str)][:5] if isinstance(ex, list) \
                    else ([str(ex)] if ex else [])
                inj = c.get("inject")
                inj = [str(x) for x in inj if isinstance(x, str)][:10] if isinstance(inj, list) else []
                corr.append({"value": str(c.get("value") or c["field"])[:40],
                             "field": str(c["field"]),
                             "handling": str(c.get("handling") or "correlated at runtime")[:200],
                             "extract": ex, "inject": inj})
        groups = []
        for g in res.get("groups", []) or []:
            if not isinstance(g, dict):
                continue
            fields = []
            for f in g.get("fields", []) or []:
                if isinstance(f, dict) and f.get("from_field") and f.get("column"):
                    col = re.sub(r"[^a-z0-9_]+", "_", str(f["column"]).lower()).strip("_")
                    fields.append({"column": col or "value",
                                   "from_field": str(f["from_field"]),
                                   "sample": str(f.get("sample") or "")[:48]})
            if fields:
                groups.append({"group": str(g.get("group") or "Data")[:24],
                               "why": str(g.get("why") or "")[:120], "fields": fields})
        _result = {"notes": (str(res["notes"])[:800] if res.get("notes") else None),
                   "correlations": corr, "groups": groups}
        llm.cache_put("aifields", _dk, _result)       # persist for later runs
        return _result
    except Exception:
        return None
