"""Locust Script Generator Agent.

Emit a clean, production-ready Locust script grounded in *actual* discovered
endpoints — never placeholder URLs. Wires CSV test data, correlates CSRF tokens
from forms, assigns journey-derived task weights, and validates every response.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

SEARCH_KEYWORDS = [
    "laptop", "shoes", "phone", "shirt", "watch", "table", "chair", "camera",
    "headphones", "backpack", "jacket", "book", "monitor", "keyboard", "mouse",
    "coffee", "lamp", "bottle", "charger", "speaker",
]
# Last-resort generic pool, used ONLY when the recording itself contains no
# discoverable search term or product id at all (see _recording_derived_samples
# below). This list is the same for every client/site, so it must never be the
# first thing tried — two different recordings (different business flows,
# different platforms) should never produce the same testdata.csv just because
# neither one had a separately-uploaded data CSV.

_SEARCH_FIELD_RE = re.compile(r"(^q$|search|keyword|query|search_?term)", re.I)
_PRODUCT_FIELD_RE = re.compile(r"(^product$|sku|product_?id|productid|item_?id|ndc)", re.I)
_PRODUCT_PATH_RE = re.compile(r"/product/(\d+)/?")


def _recording_derived_samples(discovery: dict) -> tuple[list, list]:
    """Real search terms / product ids actually observed in the uploaded
    recording — from request bodies/query fields and cart-add URL paths.

    This is what testdata.csv should fall back to BEFORE the generic
    SEARCH_KEYWORDS list when the user hasn't uploaded a separate data CSV.
    Without this, `_testdata_csv()` had no path from the recording's own
    `discovery["flow"]` to the CSV it writes at all — it only ever looked at
    a manually-uploaded `data` list — so every project fell back to the same
    hardcoded list regardless of what was actually recorded.
    """
    flow = discovery.get("flow") or []
    keywords: list[str] = []
    products: list[str] = []
    try:
        from .parameterization import _iter_fields
    except Exception:
        _iter_fields = None
    if _iter_fields:
        for name, val in _iter_fields(flow):
            v = str(val).strip()
            if not v:
                continue
            if _SEARCH_FIELD_RE.search(name) and v not in keywords:
                keywords.append(v)
            elif _PRODUCT_FIELD_RE.search(name) and v not in products:
                products.append(v)
    for s in flow:
        m = _PRODUCT_PATH_RE.search(s.get("path") or "")
        if m and m.group(1) not in products:
            products.append(m.group(1))
    return keywords[:20], products[:20]


def _business_flow_states(platform: str = "magento") -> list:
    """The ordered checkout states from the Knowledge Base Business Flow Model.
    The generated FSM is driven by THIS list rather than an implicit sequence."""
    try:
        from ..knowledge import KB
        return [s.get("state") for s in KB.business_flow(platform) if s.get("state")]
    except Exception:
        return []


def _kb_list(key: str, default) -> tuple:
    """A platform list from the KB (single source of truth), with a safe default
    so a missing/empty KB never changes behavior."""
    try:
        from ..knowledge import KB
        v = KB.magento(key)
        return tuple(v) if v else tuple(default)
    except Exception:
        return tuple(default)


def _kb_stage_map(platform: str = "") -> dict:
    """Stage for each call the generator emits itself.

    Read from the KB so it is data, not code: the `generic` block covers every
    platform because these are the TOOL's call names, and a platform block may
    override any entry. A missing or unreadable KB returns {} and the behaviour
    is exactly what it was before -- no stage rather than a wrong one.
    """
    out = {}
    try:
        from ..knowledge import KB
        for block in ("generic", (platform or "").strip().lower()):
            if not block:
                continue
            rules = KB.platform_rules(block) or {}
            m = rules.get("canonical_stages")
            if isinstance(m, dict):
                out.update({str(k): str(v) for k, v in m.items() if k and v})
    except Exception:
        return out
    return out


def _kb_test_card() -> dict:
    """A PUBLIC sandbox test card from the KB (browser_patterns) — never a real PAN."""
    try:
        import yaml
        p = Path(__file__).resolve().parent.parent / "knowledge" / "rules" / "browser_patterns.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for prov in ("stripe", "paradoxlabs_cybersource"):
            tc = ((data.get(prov) or {}).get("test_cards") or {}).get("success")
            if tc and tc.get("number"):
                return tc
    except Exception:
        pass
    return {"number": "4242424242424242", "exp": "12 / 34", "cvc": "123"}


def _swap_test_card(body, card):
    """Replace a real PAN in a recorded token-gen body with the PUBLIC test card
    (PCI: never replay a real card). Best-effort PAN-shaped swap. Returns the body
    (as a string when it was text, unchanged type when a dict is passed through)."""
    if not body:
        return body
    try:
        pan = re.sub(r"\D", "", str((card or {}).get("number") or "4242424242424242"))
        s = body if isinstance(body, str) else (json.dumps(body)
                                                if isinstance(body, (dict, list)) else str(body))
        s = re.sub(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)', pan, s)
        return s
    except Exception:
        return body


def _build_pay_api(discovery: dict, plan_cfg: dict):
    """API-replay payment config from the recording (OPT-IN, default off). Returns
    None unless plan_cfg['payment_api_replay'] is set AND the Payment Analyzer finds
    a token correlation with a recorded token endpoint. The token-gen body has its
    PAN swapped for a PUBLIC sandbox test card. Never raises."""
    try:
        if not (plan_cfg or {}).get("payment_api_replay"):
            return None
        from .payment_analyzer import _token_correlation
        corr = _token_correlation(discovery or {}, {})
        if not corr or not corr.get("token_endpoint") or not corr.get("inject"):
            return None
        tep = str(corr.get("token_endpoint") or "").lower().split("?")[0]
        tstep = None
        for s in ((discovery or {}).get("flow") or []):
            sp = str(s.get("path") or s.get("url") or "").lower()
            if tep and tep[-40:] in sp:
                tstep = s
                break
        if not tstep:
            return None
        # The hosted payment method code from the recorded order body, so the REST
        # validator uses it WITH the minted token instead of stopping on "only
        # hosted gateways".
        pay_method = None
        cep = str(corr.get("consumer_endpoint") or "").lower().split("?")[0]
        for s in ((discovery or {}).get("flow") or []):
            sp = str(s.get("path") or s.get("url") or "").lower()
            if cep and cep[-30:] in sp:
                b = s.get("body")
                bs = b if isinstance(b, str) else (json.dumps(b)
                                                   if isinstance(b, (dict, list)) else "")
                mm = re.search(r'"method"\s*:\s*"([^"]+)"', bs or "")
                if mm:
                    pay_method = mm.group(1)
                break
        return {
            "token_request": {"method": tstep.get("method", "POST"),
                              "url": tstep.get("path") or tstep.get("url"),
                              "body": _swap_test_card(tstep.get("body"), _kb_test_card())},
            "token_field": corr.get("token_field") or "id",
            "inject_field": corr.get("inject"),
            "consumer_endpoint": corr.get("consumer_endpoint"),
            "payment_method": pay_method,
            "proven": bool(corr.get("proven")),
        }
    except Exception:
        return None


def _kb_endpoints() -> dict:
    """Checkout endpoints sourced from the KB platform rules (defaults == the
    standard Magento paths, so behavior is identical if the KB is absent)."""
    m = {}
    try:
        from ..knowledge import KB
        m = KB.platform_rules("magento") or {}
    except Exception:
        m = {}
    return {
        "cart": m.get("cart_create_endpoint", "/carts/mine"),
        "items": m.get("add_item_endpoint", "/carts/mine/items"),
        "estimate_shipping": m.get("estimate_shipping_endpoint",
                                   "/carts/mine/estimate-shipping-methods-by-address-id"),
        "set_shipping": m.get("set_shipping_endpoint", "/carts/mine/shipping-information"),
        "payment_methods": m.get("payment_methods_endpoint", "/carts/mine/payment-methods"),
        "set_payment": "/carts/mine/set-payment-information",
        "place_order": m.get("place_order_endpoint", "/carts/mine/payment-information"),
        "agreements": m.get("checkout_agreements_endpoint", "/carts/mine/checkout-agreements"),
        "agreements_fallback": "/checkoutAgreements",
    }


def _ident(name: str, used: set[str]) -> str:
    """Safe python method identifier from a page label."""
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "page"
    if base[0].isdigit():
        base = "p_" + base
    cand, i = base, 1
    while cand in used:
        i += 1
        cand = f"{base}_{i}"
    used.add(cand)
    return cand


def _page_weights(discovery: dict) -> dict[str, int]:
    """Weight each page by how often it appears across weighted journeys."""
    counter: Counter[str] = Counter()
    for j in discovery.get("journeys", []):
        w = j.get("weight", 1)
        for step in j.get("steps", []):
            counter[step] += w
    if not counter:
        for p in discovery.get("pages", []):
            counter[p["name"]] += 1
    return dict(counter)


def generate(discovery: dict, plan_cfg: dict, run_dir: Path,
             credentials: dict | None = None,
             users: list | None = None, data: list | None = None,
             cart_qty: int = 1, captcha_token: str = "",
             captcha_field: str = "", payment_method: str = "",
             payment_additional_data: str = "",
             data_rows: list | None = None, faithful: bool = False,
             abort_after: int = 3, agreement_ids: list | None = None) -> dict:
    """Generate locustfile.py + testdata.csv + api_inventory.json + journey_map.md.

    `users` = [{"username","password"}, ...] from an uploaded credentials CSV.
    `data`  = [{"product_id","search_keyword"}, ...] from an uploaded data CSV.
    Uploaded values are wired into the run's testdata.csv and used by the script.
    """
    users = users or []
    data = data or []
    scripts_dir = run_dir / "scripts"
    data_dir = run_dir / "data"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Knowledge/Memory consult — apply what earlier runs on this target learned
    # (e.g. region_policy=omit_region_id) BEFORE generating. Best-effort: the
    # generator's deterministic defaults already encode these, so this never
    # changes correctness, but it surfaces the applied knowledge for the UI/log.
    try:
        from .. import memory
        _target = discovery.get("base_url") or discovery.get("domain") or ""
        discovery["_applied_memory"] = memory.facts_for(_target) if _target else {}
    except Exception:
        discovery["_applied_memory"] = {}

    pages = discovery.get("pages", []) or [{"name": "Home Page", "path": "/"}]
    weights = _page_weights(discovery)
    search = discovery.get("search_endpoint")
    login = discovery.get("login_form")
    tmin, tmax = plan_cfg["think_time"]
    product_ids = [d.get("product_id") for d in data if d.get("product_id")]
    pdp_tpl = _pdp_template(discovery) if product_ids else None

    # ---- build task methods -------------------------------------------------
    flow = discovery.get("flow")
    if flow:
        # A recording was uploaded: replay it as one correlated, asserted session.
        flow_steps = _build_flow_steps(flow)
        script = _assemble_flow_script(discovery, plan_cfg, flow_steps, tmin, tmax,
                                       cart_qty=cart_qty, captcha_token=captcha_token,
                                       captcha_field=captcha_field,
                                       payment_method=payment_method,
                                       payment_additional_data=payment_additional_data,
                                       faithful=faithful, abort_after=abort_after,
                                       agreement_ids=agreement_ids)
        scripts_index = [{"task": "recorded_flow", "label": s["name"],
                          "method": s["method"], "path": s["path"], "weight": 1}
                         for s in flow_steps]
        do_login = any(s["login"] for s in flow_steps)
    else:
        used: set[str] = set()
        task_methods: list[str] = []
        scripts_index = []
        path_by_name = {p["name"]: p.get("path", "/") for p in pages}

        for name, path in path_by_name.items():
            if search and name == "Search Results":
                continue  # handled by dedicated search task
            w = max(1, weights.get(name, 1))
            method = _ident(name, used)
            task_methods.append(_get_task(w, method, name, path))
            scripts_index.append({"task": method, "label": name, "method": "GET",
                                  "path": path, "weight": w})

        if search:
            w = max(1, weights.get("Search Results", 5))
            method = _ident("search_keyword", used)
            task_methods.append(_search_task(w, method, search))
            scripts_index.append({"task": method, "label": "Search Results",
                                  "method": search.get("method", "GET"),
                                  "path": search.get("action", "/"), "weight": w})

        if pdp_tpl:
            method = _ident("product_by_id", used)
            task_methods.append(_product_task(8, method, pdp_tpl))
            scripts_index.append({"task": method, "label": "Product by ID",
                                  "method": "GET",
                                  "path": f"{pdp_tpl[0]}<product_id>{pdp_tpl[1]}", "weight": 8})

        do_login = bool(login and (credentials or users))
        script = _assemble_script(discovery, plan_cfg, task_methods, tmin, tmax,
                                  do_login, login, search)

    # ---- write artifacts ----------------------------------------------------
    (scripts_dir / "locustfile.py").write_text(script, encoding="utf-8")

    csv_path = data_dir / "testdata.csv"
    if data_rows:
        # preserve EVERY uploaded column (card, shipping, billing, etc.) verbatim
        csv_path.write_text(_rows_to_csv(data_rows), encoding="utf-8")
    else:
        rec_keywords, rec_products = _recording_derived_samples(discovery)
        csv_path.write_text(
            _testdata_csv(credentials, users, data,
                          recorded_keywords=rec_keywords,
                          recorded_products=rec_products),
            encoding="utf-8")

    inventory = {
        "base_url": discovery.get("base_url"),
        "domain": discovery.get("domain"),
        "tech": discovery.get("tech"),
        "pages": pages,
        "forms": discovery.get("forms", []),
        "apis": discovery.get("apis", []),
        "search_endpoint": search,
    }
    (run_dir / "api_inventory.json").write_text(json.dumps(inventory, indent=2),
                                                encoding="utf-8")
    (run_dir / "journey_map.md").write_text(_journey_md(discovery), encoding="utf-8")

    # persist the discovery so a saved script can be REGENERATED later with the
    # current generator (always up to date), instead of frozen as a stale script
    try:
        (run_dir / "discovery.json").write_text(
            json.dumps(discovery, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass

    # label -> endpoint map so the report can show the endpoint per transaction
    endpoints_map = {s["label"]: {"method": s.get("method", "GET"), "path": s.get("path", "")}
                     for s in scripts_index}
    (run_dir / "endpoints_map.json").write_text(json.dumps(endpoints_map, indent=2),
                                                encoding="utf-8")

    return {
        "script": script,
        "script_path": str(scripts_dir / "locustfile.py"),
        "testdata_path": str(csv_path),
        "scripts_index": scripts_index,
        "do_login": do_login,
    }


# --------------------------------------------------------------------------- #
# script fragments
# --------------------------------------------------------------------------- #
def _get_task(weight: int, method: str, label: str, path: str) -> str:
    safe_path = path if path.startswith("/") else "/" + path
    return f'''
    @task({weight})
    def {method}(self):
        with self.client.get(
            {safe_path!r},
            name={label!r},
            catch_response=True,
        ) as resp:
            if resp.status_code >= 400:
                resp.failure(f"[{label}] code={{resp.status_code}} url={{resp.url}} "
                             f"body={{resp.text[:150]}}")
            else:
                resp.success()
'''


def _search_task(weight: int, method: str, search: dict) -> str:
    raw = str(search.get("action") or "/")
    action = raw if raw.startswith("/") else "/" + raw
    param = search.get("param", "q") or "q"
    http = "post" if search.get("method", "GET").upper() == "POST" else "get"
    if http == "get":
        call = (f'self.client.get(f"{action}?{param}={{kw}}", '
                f'name="Search Results", catch_response=True)')
    else:
        call = (f'self.client.post("{action}", data={{{param!r}: kw}}, '
                f'name="Search Results", catch_response=True)')
    return f'''
    @task({weight})
    def {method}(self):
        kw = random.choice(self.search_keywords)
        with {call} as resp:
            if resp.status_code >= 400:
                resp.failure(f"[Search Results] code={{resp.status_code}} q={{kw}} "
                             f"url={{resp.url}}")
            else:
                resp.success()
'''


def _login_block(login: dict, search) -> str:
    action = login["full_action"] if login else "/login"
    action_path = login["action"] if login else "/login"
    # detect user/password field names
    user_field, pass_field = "username", "password"
    for i in login.get("inputs", []) if login else []:
        n = i.get("name", "").lower()
        if any(x in n for x in ("user", "email", "login")) and "pass" not in n:
            user_field = i["name"]
        if "pass" in n:
            pass_field = i["name"]
    return f'''
    def _login(self):
        """Correlate CSRF token from the login form, then authenticate."""
        creds = random.choice(self.credentials) if self.credentials else None
        if not creds:
            return
        token = None
        with self.client.get({action_path!r}, name="Login Page",
                             catch_response=True) as page:
            token = self._extract_token(page.text)
            page.success()
        payload = {{{user_field!r}: creds.get("username", ""),
                   {pass_field!r}: creds.get("password", "")}}
        for key in ("csrf_token", "form_key", "_token", "authenticity_token",
                    "csrfmiddlewaretoken"):
            if token:
                payload[key] = token
        with self.client.post({action_path!r}, data=payload, name="Login Submit",
                             catch_response=True) as resp:
            if resp.status_code >= 400:
                resp.failure(f"[Login Submit] code={{resp.status_code}}")
            else:
                resp.success()

    @staticmethod
    def _extract_token(html):
        import re as _re
        for pat in (r'name="csrf_token"[^>]*value="([^"]+)"',
                    r'name="form_key"[^>]*value="([^"]+)"',
                    r'name="_token"[^>]*value="([^"]+)"',
                    r'name="authenticity_token"[^>]*value="([^"]+)"',
                    r'name="csrfmiddlewaretoken"[^>]*value="([^"]+)"'):
            m = _re.search(pat, html or "")
            if m:
                return m.group(1)
        return None
'''


def _assemble_script(discovery, plan_cfg, task_methods, tmin, tmax,
                     do_login, login, search) -> str:
    _base = discovery.get("base_url")
    _domain = discovery.get("domain")
    _tech = ", ".join(discovery.get("tech") or ["unknown"])
    _label = plan_cfg["label"]
    _users = plan_cfg["users"]
    _dur = plan_cfg["duration_human"]
    _durs = plan_cfg["duration_s"]
    _spawn = plan_cfg["spawn_rate"]
    header = f'''"""
LT Metrics-generated Locust script.
Target : {_base}
Domain : {_domain}
Tech   : {_tech}
Profile: {_label} ({_users} users, {_dur})

Auto-generated — grounded in crawled endpoints. Do not hand-edit unless needed.
Run with the LT Metrics UI, or directly:
    locust -f locustfile.py --host {_base} \\
           --users {_users} --spawn-rate {_spawn} \\
           --run-time {_durs}s --headless
"""
import csv
import os
import random

from locust import HttpUser, task, between, constant_pacing, constant_throughput, events

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "testdata.csv")


def _load_testdata():
    keywords, creds, products = [], [], []
    try:
        with open(_DATA, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("search_keyword"):
                    keywords.append(row["search_keyword"].strip())
                if row.get("product_id"):
                    products.append(row["product_id"].strip())
                if row.get("username"):
                    creds.append({{"username": row["username"].strip(),
                                   "password": (row.get("password") or "").strip()}})
    except FileNotFoundError:
        pass
    if not keywords:
        keywords = ["laptop", "shoes", "phone", "book", "camera"]
    return keywords, creds, products


class WebsiteUser(HttpUser):
    """A simulated visitor exercising the discovered user journeys."""
    wait_time = between({tmin}, {tmax})

    def on_start(self):
        self.search_keywords, self.credentials, self.product_ids = _load_testdata()
'''
    if do_login:
        header += "        self._login()\n"

    body = "".join(task_methods)
    if search and "self.search_keywords" not in body:
        pass
    login_code = _login_block(login, search) if do_login else ""

    return header + body + login_code + '''

@events.quitting.add_listener
def _log_summary(environment, **kwargs):
    stats = environment.stats.total
    fail_ratio = stats.fail_ratio * 100 if stats.num_requests else 0
    print(f"\\n[LT Metrics] Requests={stats.num_requests} Failures={stats.num_failures} "
          f"({fail_ratio:.2f}%) p95={stats.get_response_time_percentile(0.95)}ms")
'''


def _csv_cell(v) -> str:
    v = "" if v is None else str(v)
    if any(c in v for c in (",", '"', "\n")):
        return '"' + v.replace('"', '""') + '"'
    return v


def _rows_to_csv(rows: list) -> str:
    """Serialize uploaded rows verbatim (union of all columns, order preserved)."""
    cols = []
    for r in rows:
        for k in (r or {}).keys():
            if k and k not in cols:
                cols.append(k)
    if not cols:
        return "username,password,search_keyword,product_id\n"
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(_csv_cell((r or {}).get(c, "")) for c in cols))
    return "\n".join(lines) + "\n"


def _testdata_csv(credentials: dict | None, users: list | None = None,
                  data: list | None = None,
                  recorded_keywords: list | None = None,
                  recorded_products: list | None = None) -> str:
    """Build the run's testdata.csv from uploaded users + product/search data.

    Columns: username,password,search_keyword,product_id — all optional per row.

    Fallback order for search_keyword/product_id, when no per-row `data` value
    is present: (1) `recorded_keywords`/`recorded_products` — real values pulled
    from THIS recording via `_recording_derived_samples()` — then (2) the
    generic `SEARCH_KEYWORDS` list, only if the recording itself had nothing
    discoverable. Skipping straight to (2) is what caused unrelated clients to
    end up with identical test data.
    """
    users = list(users or [])
    data = list(data or [])
    if credentials and not users:
        users = [{"username": credentials.get("username", ""),
                  "password": credentials.get("password", "")}]

    keywords = [d.get("search_keyword", "") for d in data if d.get("search_keyword")]
    products = [d.get("product_id", "") for d in data if d.get("product_id")]
    if not keywords:
        keywords = list(recorded_keywords or []) or list(SEARCH_KEYWORDS)
    if not products:
        products = list(recorded_products or [])

    n = max(len(users), len(keywords), len(products), 1)
    lines = ["username,password,search_keyword,product_id"]
    for i in range(n):
        u = users[i] if i < len(users) else {}
        kw = keywords[i % len(keywords)] if keywords else ""
        pid = products[i % len(products)] if products else ""
        lines.append(",".join(_csv_cell(x) for x in (
            u.get("username", ""), u.get("password", ""), kw, pid)))
    return "\n".join(lines) + "\n"


def _pdp_template(discovery: dict):
    """Derive a (prefix, suffix) around a product-id slot from a PDP path."""
    for p in discovery.get("pages", []):
        name = p.get("name", "")
        path = p.get("path", "/") or "/"
        if "PDP" in name or "product" in path.lower():
            m = re.search(r"\d+", path)
            if m:
                return path[:m.start()], path[m.end():]
            return path.rstrip("/") + "/", ""
    return None


def _product_task(weight: int, method: str, tpl: tuple) -> str:
    prefix, suffix = tpl
    return f'''
    @task({weight})
    def {method}(self):
        if not self.product_ids:
            return
        pid = random.choice(self.product_ids)
        url = f"{prefix}{{pid}}{suffix}"
        with self.client.get(url, name="Product by ID", catch_response=True) as resp:
            if resp.status_code >= 400:
                resp.failure(f"[Product by ID] code={{resp.status_code}} id={{pid}} "
                             f"url={{resp.url}}")
            else:
                resp.success()
'''


def _build_flow_steps(flow: list) -> list:
    """Normalize recorded steps: detect the login step, ensure unique names."""
    steps, seen = [], set()
    for s in flow:
        method = (s.get("method") or "GET").upper()
        path = s.get("path", "/") or "/"
        low = path.lower()
        is_login = (method == "POST" and ("account/login" in low or low.endswith("/login"))
                    and "validate" not in low)
        name = "Login" if is_login else (s.get("label") or f"{method} {path}")
        base, i = name, 1
        while name in seen and not is_login:
            i += 1
            name = f"{base} #{i}"
        seen.add(name)
        steps.append({"method": method, "path": path, "name": name,
                      "group": s.get("group", "") or "",   # business group (Taurus transaction)
                      "body": s.get("body"), "asserts": s.get("asserts", []) or [],
                      "login": is_login, "xhr": s.get("xhr", False),
                      "json": s.get("json", False), "rest": s.get("rest", False)})
    return steps


def _browse_task_methods(discovery: dict, browse_total: int) -> str:
    """Generate lightweight browse @task methods (home / search / PDP) whose
    weights sum to `browse_total`, so the run mixes Browse% + Checkout%."""
    if browse_total < 1:
        return ""
    pages = discovery.get("pages") or []
    pdp = next((p.get("path") for p in pages
                if "PDP" in (p.get("name") or "") or "/product" in (p.get("path") or "").lower()), None)
    if not pdp:
        pdp = next((p.get("path") for p in pages
                    if p.get("path") and p.get("path") != "/"
                    and str(p.get("path")).endswith(".html")), None)
    search = discovery.get("search_endpoint") or {}
    action = search.get("action") or "/catalogsearch/result/"
    param = search.get("param") or "q"
    prefix = action + ("&" if "?" in action else "?") + param + "="

    entries = [("browse_home", "get", "/", "Home Page"),
               ("browse_search", "search", prefix, "Search Results")]
    if pdp:
        entries.append(("browse_pdp", "get", pdp, "Product Detail (PDP)"))
    each = max(1, round(browse_total / len(entries)))

    L = []
    for name, kind, target, label in entries:
        L.append("    @task(%d)" % each)
        L.append("    def %s(self):" % name)
        if kind == "search":
            L.append("        terms = self.search_keywords or self.product_ids")
            L.append("        if not terms:")
            L.append("            return")
            L.append("        from urllib.parse import quote_plus as _qp")
            L.append("        _u = %r + _qp(str(random.choice(terms)))" % target)
        else:
            L.append("        _u = %r" % target)
        L.append("        with self.client.get(_u, name=%r, catch_response=True) as r:" % label)
        L.append("            r.success() if r.status_code < 400 else r.failure(%r + str(r.status_code))"
                 % (label + " "))
        L.append("")
    return "\n".join(L)


def _derive_rest_prefix(path: str) -> str | None:
    """Return the full REST base for a Magento REST path, preserving any leading
    store-code path segment.

        /uk/rest/uk/V1/carts/mine/totals -> /uk/rest/uk/V1   (store-code URL)
        /rest/default/V1/carts/mine       -> /rest/default/V1
        /rest/V1/products                 -> /rest/V1
    """
    idx = path.find("/rest/")
    if idx == -1:
        return None
    before = path[:idx]                       # e.g. "/uk" or ""
    after = path[idx + len("/rest/"):].split("/")
    if not after:
        return None
    if re.match(r"V\d+$", after[0]):          # /rest/V1/...
        ver = [after[0]]
    elif len(after) >= 2 and re.match(r"V\d+$", after[1]):   # /rest/<store>/V1/...
        ver = [after[0], after[1]]
    else:
        ver = after[:1]
    return before + "/rest/" + "/".join(ver)


def _detect_payment_method_from_recording(flow_steps: list) -> dict:
    """Intelligently detect payment method from the recording.

    Returns dict with:
    - 'method': detected payment method code (e.g., 'paradoxlabs_cybersource')
    - 'gateway_type': 'hosted' or 'offline'
    - 'gateway_name': human-readable gateway name
    - 'confidence': 'high', 'medium', or 'low'
    - 'reason': explanation of detection
    """
    # Known hosted payment gateways (require iframe/browser automation)
    HOSTED_GATEWAYS = {
        'paradoxlabs_cybersource': ('CyberSource', 'cybersource'),
        'stripe_payments': ('Stripe', 'stripe'),
        'braintree': ('Braintree', 'braintree'),
        'adyen': ('Adyen', 'adyen'),
        'authorizenet': ('Authorize.net', 'authorizenet'),
        'paypal_express': ('PayPal Express', 'paypal'),
    }

    # Known offline payment methods (work via HTTP)
    OFFLINE_METHODS = {
        'netterms', 'purchaseorder', 'checkmo', 'banktransfer',
        'cashondelivery', 'free', 'companycredit', 'check', 'moneyorder'
    }

    detected = {
        'method': '',
        'gateway_type': None,
        'gateway_name': None,
        'confidence': 'low',
        'reason': 'No payment method detected'
    }

    # Search for payment method references in the recording
    all_text = " ".join(json.dumps(s) if isinstance(s, dict) else str(s) for s in flow_steps)

    # 1) Check for hosted gateway names/codes
    for gateway_code, (gateway_name, search_terms) in HOSTED_GATEWAYS.items():
        for term in [gateway_code, search_terms] + [gateway_name.lower().replace(' ', '')]:
            if term.lower() in all_text.lower():
                detected = {
                    'method': gateway_code,
                    'gateway_type': 'hosted',
                    'gateway_name': gateway_name,
                    'confidence': 'high' if gateway_code in all_text else 'medium',
                    'reason': f'Detected {gateway_name} in recording'
                }
                return detected

    # 2) Check for "paymentMethod": {"method": "X"} patterns
    for step in flow_steps:
        body_text = json.dumps(step.get('body', '')) if isinstance(step.get('body'), dict) else str(step.get('body', ''))
        pm_match = re.search(r'"(?:method|paymentMethod)"["\s:]*"([^"]+)"', body_text)
        if pm_match:
            method_code = pm_match.group(1).lower()

            # Check if it's a known hosted gateway
            for gateway_code, (gateway_name, _) in HOSTED_GATEWAYS.items():
                if gateway_code.lower() in method_code or gateway_name.lower() in method_code:
                    detected = {
                        'method': gateway_code,
                        'gateway_type': 'hosted',
                        'gateway_name': gateway_name,
                        'confidence': 'high',
                        'reason': f'Found paymentMethod: {gateway_code} in recording'
                    }
                    return detected

            # Check if it's offline
            if any(m in method_code for m in OFFLINE_METHODS):
                detected = {
                    'method': method_code,
                    'gateway_type': 'offline',
                    'gateway_name': method_code,
                    'confidence': 'high',
                    'reason': f'Found offline payment method: {method_code}'
                }
                return detected

    # 3) Check for "secureAccept", "3D Secure", "hosted gateway" patterns
    if 'secureaccept' in all_text.lower() or '3d secure' in all_text.lower():
        detected = {
            'method': 'paradoxlabs_cybersource',
            'gateway_type': 'hosted',
            'gateway_name': 'CyberSource',
            'confidence': 'high',
            'reason': 'Detected CyberSource 3D Secure flow (secureAccept) in recording'
        }
        return detected

    return detected


def _wait_time_expr(plan_cfg: dict, tmin, tmax) -> str:
    """The Locust wait strategy to emit, from the requested pacing.

    Think time alone makes throughput an OUTPUT of the run: when the system
    slows, each user completes fewer iterations, so offered load falls exactly
    when the system is under stress. That is backwards for capacity work, where
    the question is "hold this rate and tell me what breaks".

    target_tps   iterations per SECOND across all users -> constant_throughput,
                 divided per user because Locust applies it per user.
    pacing_s     each iteration takes at least this many seconds in total
                 (request time included) -> constant_pacing.
    neither      unchanged: think time between iterations.

    A target the application cannot sustain is not enforced upward -- Locust
    cannot make a slow response faster -- so the achieved rate in the report is
    still the truth. That IS the finding when it happens.
    """
    users = max(1, int(plan_cfg.get("users") or 1))
    tps = plan_cfg.get("target_tps")
    pacing = plan_cfg.get("pacing_s")
    try:
        if tps and float(tps) > 0:
            per_user = float(tps) / users
            return "constant_throughput(%.6g)" % per_user
        if pacing and float(pacing) > 0:
            return "constant_pacing(%.6g)" % float(pacing)
    except (TypeError, ValueError):
        pass
    return "between(%s, %s)" % (tmin, tmax)


def _assemble_flow_script(discovery: dict, plan_cfg: dict, flow_steps: list,
                          tmin, tmax, cart_qty: int = 1,
                          captcha_token: str = "", captcha_field: str = "",
                          payment_method: str = "",
                          payment_additional_data: str = "", faithful: bool = False,
                          abort_after: int = 3, agreement_ids: list | None = None) -> str:
    """Emit a Locust script that replays the recording as one correlated session.

    Built via token replacement (not an f-string) to keep the generated code
    free of brace-escaping hazards.
    """
    # Login endpoints are derived from the crawled site (login form action),
    # with standard-Magento fallbacks. Nothing site-specific is hardcoded.
    login_urls = []
    _login_action = (discovery.get("login_form") or {}).get("action")
    if _login_action:
        login_urls.append(_login_action)
    # Multi-store Magento serves each store view under a path prefix (Radwell's
    # UK store is /uk/...). The fallback login endpoints must carry that prefix,
    # or the storefront session is authenticated on the DEFAULT store while every
    # storefront call in the journey runs on /uk as a GUEST. The visible symptom
    # is silent: /uk/checkout/cart/add succeeds (200) but adds to a guest quote,
    # so the customer's REST quote stays empty and the line lands at price 0.
    # Same root cause the rest_prefix derivation below already guards against.
    _store_pfx = ""
    for _s in flow_steps:
        if _s.get("rest"):
            _rp = _derive_rest_prefix(_s.get("path", "")) or ""
            _cut = _rp.find("/rest/")
            if _cut > 0:
                _store_pfx = _rp[:_cut]
                break
    _fallbacks = ["/customer/account/loginPost", "/customer/ajax/login"]
    if _store_pfx:
        _fallbacks = [_store_pfx + _g for _g in _fallbacks] + _fallbacks
    for _g in _fallbacks:
        if _g not in login_urls:
            login_urls.append(_g)
    # REST store prefix derived from the actual /rest/<store>/V<n> the site uses
    # Derive the REST base INCLUDING any leading store-code path (e.g. Radwell's
    # /uk/rest/uk/V1). Matching only "/rest/.." would drop the "/uk" and every
    # carts/mine/* call would 401 ("consumer isn't authorized").
    rest_prefix = "/rest/V1"
    for _s in flow_steps:
        if _s.get("rest"):
            _rp = _derive_rest_prefix(_s.get("path", ""))
            if _rp:
                rest_prefix = _rp
                break
    # apply optional AI self-repair hints (safe, bounded knobs only)
    hints = discovery.get("repair_hints") or {}
    for _lu in (hints.get("login_urls") or []):
        if isinstance(_lu, str) and _lu and _lu not in login_urls:
            login_urls.insert(0, _lu)
    if hints.get("rest_prefix"):
        rest_prefix = str(hints["rest_prefix"])
    _eh = hints.get("extra_headers")
    extra_headers = ({str(k): str(v) for k, v in _eh.items()}
                     if isinstance(_eh, dict) else {})

    # INTELLIGENT PAYMENT METHOD SELECTION:
    # 1) User-supplied payment method (explicit override)
    # 2) AI repair hint (from previous analysis)
    # 3) Recording-detected payment method (NEW - DYNAMIC at runtime)
    forced_payment = str(payment_method or hints.get("payment_method") or "")

    if not forced_payment:
        # Auto-detect payment method from recording to inform runtime selection
        detected = _detect_payment_method_from_recording(flow_steps)
        if detected['method']:
            # Store detection info in comment for generated script
            # Script will use this at runtime to make dynamic decisions
            print(f"[LT Metrics Generator] Detected payment method: {detected['method']} "
                  f"({detected['gateway_type']}, {detected['confidence']} confidence). "
                  f"Script will dynamically select method at runtime based on what's available.")

            if detected['gateway_type'] == 'hosted':
                # For hosted gateways, leave empty so runtime can decide
                # The generated script will prefer offline methods if available,
                # or use hosted method with proper handling
                forced_payment = ""
                print(f"[LT Metrics Generator] Hosting gateway detected ({detected['gateway_name']}). "
                      f"Script will dynamically choose best method at runtime.")
            else:
                # For offline methods, keep them but allow runtime override
                forced_payment = detected['method']
                print(f"[LT Metrics Generator] Offline method detected: {forced_payment}. "
                      f"Script will use this unless overridden at runtime.")
        else:
            # No payment method detected, leave empty for dynamic selection
            print(f"[LT Metrics Generator] No specific payment method detected. "
                  f"Script will dynamically select from available methods at runtime.")
            forced_payment = ""
    payment_addl = {}
    if payment_additional_data:
        try:
            _pa = json.loads(payment_additional_data)
            if isinstance(_pa, dict):
                payment_addl = _pa
        except Exception:
            payment_addl = {}

    # Generic parameterization map: recorded body field name -> CSV column.
    # Derived from the recording itself (any platform). Credentials are handled
    # separately (email/password), so they're excluded here.
    param_map, corr_rules = {}, []
    try:
        from . import parameterization
        _an = parameterization.analyze(flow_steps)
        for _g in _an.get("groups", []):
            if _g.get("group") == "Credentials":
                continue
            for _f in _g.get("fields", []):
                if _f.get("from_field") and _f.get("column"):
                    param_map[_f["from_field"]] = _f["column"]
        corr_rules = parameterization.correlation_rules(_an)
    except Exception:
        param_map, corr_rules = {}, []

    # Harvest real SKUs from the recording so the empty-cart auto-heal can add a
    # REST cart item even without an uploaded product CSV (search q= terms + any
    # "sku" in bodies). These are product codes (SKUs), not numeric entity ids.
    from urllib.parse import unquote_plus as _uqp
    rest_skus = []
    for _s in flow_steps:
        _mp = re.search(r"[?&]q=([^&]+)", _s.get("path") or "")
        if _mp:
            rest_skus.append(_uqp(_mp.group(1)))
        _b = _s.get("body")
        _bs = _b if isinstance(_b, str) else (json.dumps(_b) if isinstance(_b, dict) else "")
        for _mm in re.finditer(r'"sku"\s*:\s*"([^"]+)"', _bs or ""):
            rest_skus.append(_mm.group(1))
    rest_skus = list(dict.fromkeys([x for x in rest_skus if x]))[:20]
    # Does the flow do REST (carts/mine) checkout without ever recording a REST
    # cart-add? Then the token cart will be empty → add one proactively.
    _allpaths = " ".join((s.get("path") or "").lower() for s in flow_steps)
    needs_rest_cart = (("carts/mine/shipping-information" in _allpaths
                        or "estimate-shipping" in _allpaths)
                       and "carts/mine/items" not in _allpaths)

    tmpl = r'''"""
LT Metrics-generated Locust script (recorded checkout flow).
Target : __BASE__
Domain : __DOMAIN__
Profile: __LABEL__ (__USERS__ users, __DUR__)
Steps  : __NSTEPS__ recorded transactions, replayed sequentially in one session
         with fresh form_key correlation and CSV-driven login.
"""
import csv
import json
import os
import random
import re
import threading
import time

from locust import HttpUser, task, between, constant_pacing, constant_throughput, events

_RUN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = os.path.join(_RUN_DIR, "data", "testdata.csv")
_STATS_PATH = os.path.join(_RUN_DIR, "results", "ltm_flow.json")

_LTM_BUILD = "__BUILD__"          # generation timestamp — confirms which script is running
# Memory applied to THIS build: facts earlier runs on this target taught LT Metrics
# (e.g. region_policy, payment_method). Proves Run N+1 uses Run N's lesson with
# no LLM. Empty on a target's first run.
_APPLIED_MEMORY = __APPLIED_MEMORY__
# Business Flow Model — the ordered checkout states this run must pass through,
# SOURCED FROM the Knowledge Base (patterns.yaml), not hard-coded here. The
# validator drives these states in order and fails fast on the first broken one.
_FLOW_MODEL = __FLOW_MODEL__
_FLOW = {"login_ok": 0, "login_fail": 0, "orders": 0, "heals": 0, "captcha": 0,
         "order_ids": [], "build": _LTM_BUILD, "applied_memory": _APPLIED_MEMORY,
         "flow_model": _FLOW_MODEL}
_LOCK = threading.Lock()
_HAS_REST = __HAS_REST__
# JMeter-style ORDER PATH — opt in with env var LTM_RECORDED_CHECKOUT=1.
# By default a Magento REST target places the order through LT Metrics's own
# bearer-token REST sequence, which builds its OWN cart. That is robust, but on
# a store which prices the line in a custom STOREFRONT module the REST cart is
# never priced, so every order captures shipping/tax only and the recorded
# storefront cart is discarded. Setting this replays the RECORDED checkout
# instead — still fully correlated and parameterized, unlike faithful mode which
# drops correlation and goes stale on form_key — so the order is placed from the
# same session cart the storefront priced. This is how a correlated JMeter
# script behaves. Trade-off: the recorded checkout POSTs are more brittle than
# the validator, which is why they are not the default.
_RECORDED_CHECKOUT = (str(os.getenv("LTM_RECORDED_CHECKOUT", "")).strip().lower()
                      not in ("", "0", "false", "no"))
_HAS_LOGIN_STEP = __HAS_LOGIN_STEP__
LOGIN_URLS = __LOGIN_URLS__
_REST_PREFIX = __REST_PREFIX__
# Store view + GraphQL endpoint derived from the REST prefix. GraphQL is PUBLIC
# (no admin token) — unlike the admin-scoped /V1/products endpoint — so it is the
# right way to resolve a search term -> sku + stock with only a customer session.
_REST_PARTS = [p for p in (_REST_PREFIX or "").strip("/").split("/") if p]
_STORE = _REST_PARTS[0] if len(_REST_PARTS) >= 2 and _REST_PARTS[1] == "rest" else ""
_GRAPHQL_URL = ("/" + _STORE + "/graphql") if _STORE else "/graphql"
_CART_QTY = __CART_QTY__          # units added per add-to-cart (raises product load)
_REST_SKUS = __REST_SKUS__        # SKUs harvested from the recording (empty-cart heal fallback)
_NEEDS_REST_CART = __NEEDS_REST_CART__   # flow does REST checkout but never records a REST cart-add
_FAITHFUL = __FAITHFUL__          # JMeter-style: replay recorded steps verbatim (no parameterize/correlate/heal)
_FAITHFUL_FORCED_OFF = __FAITHFUL_FORCED_OFF__   # faithful was requested but auto-switched to rest-checkout (recording has REST carts/mine write calls that can't be replayed verbatim)
# STRICT / REPRODUCIBLE mode (opt-in; default False). A performance tool must be a
# stable ruler: the same test run twice should apply the SAME load so results are
# comparable across releases. When _STRICT is True the generated script:
#   * does NOT self-heal (retries/adaptations are logged, not performed);
#   * does NOT auto-pick or auto-switch the payment method (Memory / offline-preference
#     / hosted-fallback are all disabled) — it uses ONLY an explicit forced method or
#     the CSV's method, and fails LOUDLY if neither is usable;
#   * stamps the effective profile into the report so what ran is unambiguous.
# Default (False) keeps all the adaptive, best-effort behaviour unchanged.
_STRICT = __STRICT__
_PAY_API = __PAY_API__            # API-replay payment config (mint token + inject via correlation) or None. Opt-in; None = inert.
# Abort the WHOLE run when checkout is definitively broken: after this many
# fail-fast checkout stops with zero orders, stop the Locust runner instead of
# hammering a broken checkout for the full duration. 0 disables (pure load mode).
_ABORT_AFTER = __ABORT_AFTER__
# Checkout endpoints + payment policy — sourced from the Knowledge Base at
# generation time (single source of truth), not hardcoded here.
_EP = __ENDPOINTS__
# Checkout-agreement (T&C) ids harvested from the recording (stable store config).
# The REST agreements endpoints are frequently not exposed (404), so these are the
# primary source; the REST fetch is only a fallback.
_AGREEMENT_IDS = __AGREEMENT_IDS__
_ADD_CART_SIGNALS = ("cart/add", "carts/mine/items", "add-to-cart", "add_to_cart",
                     "cart/add.js", "cart/change.js", "add_item")
# CAPTCHA handling. A CAPTCHA cannot be solved by an HTTP load test by design;
# the correct approach is to DISABLE it in the test env, use the provider's test
# keys, or allowlist the load-generator IPs. When the env issues a bypass/test
# token, set it here and it is injected into login + checkout POST bodies.
_CAPTCHA_TOKEN = __CAPTCHA_TOKEN__     # bypass / reCAPTCHA test-key response, or ""
_CAPTCHA_FIELD = __CAPTCHA_FIELD__     # extra field name to carry the token, or ""
# Fixes proposed by the optional AI self-repair pass (empty unless it ran).
_EXTRA_HEADERS = __EXTRA_HEADERS__     # headers added to every checkout request
# PAYMENT METHOD STRATEGY (from recording analysis):
# __PAYMENT_METHOD_DETECTION_REASON__
# If _FORCED_PAYMENT is empty, the script will dynamically select payment methods
# at runtime based on what the target site offers:
# 1) Prefer offline methods (netterms, purchaseorder, etc.) - pure HTTP, scalable
# 2) Fall back to non-hosted methods if offline unavailable
# 3) Use hosted gateway (CyberSource, Stripe) if that's all available
#    (requires --browser-payment flag for iframe automation, or manual token setup)
# To force a specific method: set _FORCED_PAYMENT = 'method_code'
_FORCED_PAYMENT = __FORCED_PAYMENT__   # payment method code to force at place-order (empty = dynamic selection)
# Gateway test/sandbox-mode payment params. When the gateway is in test mode and
# a stored-card token / test profile is used, these are injected into the
# payment/place-order call so a real (test) card order completes under load —
# without a fresh single-use iframe token.
_PAYMENT_ADDL = __PAYMENT_ADDL__       # dict merged into paymentMethod.additional_data
# DECLARED payment intent. True when the test data asks for a card gateway AND the
# additional_data references per-user values ({{csv_column}}). When true, a user
# whose row cannot supply those values FAILS the run rather than silently paying by
# an offline method -- see _payment_intent_unmet.
# True when the additional_data references per-user values. Whether a given ROW
# actually intends a card is decided per row, because _FORCED_PAYMENT may name a
# column -- see _row_payment_method.
_ADDL_IS_PER_USER = any(
    isinstance(v, str) and "{{" in v for v in (_PAYMENT_ADDL or {}).values())
# Stop each user after this many completed orders. 0 = no cap, run for the
# duration. Locust in this build has no --iterations, and a duration cannot
# express "one order each": it loops until the clock runs out, so the count
# depends on how fast the store happens to be that minute. A smoke test wants a
# known number of orders, not a number discovered afterwards.
_MAX_ORDERS_PER_USER = __MAX_ORDERS_PER_USER__
_PARAM_MAP = __PARAM_MAP__             # {recorded field name: CSV column} generic parameterization
_CORRELATIONS = __CORRELATIONS__       # JMeter-style extractor rules (capture from response, inject into request)
# API-call GROUPS (recorded Taurus transactions) + per-group Percent Executions.
# _GROUP_PCT maps group -> % of iterations that run that group (100 = always).
# _NAME_GROUP maps a request name -> its stage, for tagging the live call log.
#
# A transaction row is an AGGREGATE, not a call: `TXN: Checkout` spans every
# request inside it, so its duration already includes theirs. Labelling it
# "TXN" keeps it out of a per-stage time sum, where it would otherwise count
# the same milliseconds twice.
_GROUP_PCT = __GROUP_PCT__
_NAME_GROUP = __NAME_GROUP__


def _stage_of(name):
    """The stage a logged row belongs to, or "TXN" for a transaction timer."""
    n = name or ""
    if n.startswith("TXN: "):
        return "TXN"
    return _NAME_GROUP.get(n, "")
# Live per-request feed (JMeter "View Results Tree"): every request is appended
# here as one JSON line so the UI can stream request/response/status live.
_CALLS_PATH = os.path.join(_RUN_DIR, "results", "ltm_calls.jsonl")
_CALL_SEQ = [0]
_CALLS_CAP = 20000                     # bound disk/memory: stop after this many detailed calls
try:
    os.makedirs(os.path.dirname(_CALLS_PATH), exist_ok=True)
except Exception:
    pass
_SECRET_RE = re.compile(
    r'("(?:password|passwd|pwd|cvv|cvn|card_?number|cc_?number|securitycode|'
    r'payment_token|token|authorization|access_token|client_secret)"\s*:\s*")[^"]*', re.I)


def _redact(s, n=2000):
    """Truncate a request/response body to ~2KB and mask secrets for the live feed."""
    if s is None:
        return ""
    try:
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", "ignore")
        elif not isinstance(s, str):
            s = str(s)
    except Exception:
        return ""
    try:
        s = _SECRET_RE.sub(lambda m: m.group(1) + "***", s)
        s = re.sub(r'(Bearer\s+)[A-Za-z0-9._\-]+', r'\1***', s)
    except Exception:
        pass
    return s[:n]


def _roll_groups():
    """Per-iteration active set of groups by Percent Executions. None => no gating
    (nothing grouped). A group at 100% always runs; at 0% never runs."""
    if not _GROUP_PCT:
        return None
    active = set()
    for g, p in _GROUP_PCT.items():
        try:
            p = float(p)
        except (TypeError, ValueError):
            p = 100.0
        if p >= 100 or random.uniform(0, 100) < p:
            active.add(g)
    return active


def _grp_active(step, active):
    """Should this step run this iteration? Ungrouped/unknown steps always run."""
    if active is None:
        return True
    g = step.get("group")
    if not g or g not in _GROUP_PCT:
        return True
    return g in active


def _set_fields(body, fieldmap):
    """Overwrite body fields whose name is a key in `fieldmap` with that value.
    Works on dict, JSON-string and form-encoded bodies. Returns the same type it
    received; never mutates the input."""
    if not fieldmap:
        return body

    def _set(obj):
        if isinstance(obj, dict):
            for k in list(obj):
                if k in fieldmap and not isinstance(obj[k], (dict, list)) \
                        and str(fieldmap[k]) != "":
                    obj[k] = fieldmap[k]
                _set(obj[k])
        elif isinstance(obj, list):
            for it in obj:
                _set(it)

    if isinstance(body, str):
        b = body.strip()
        if b.startswith("{"):
            try:
                obj = json.loads(b)
                _set(obj)
                return json.dumps(obj)
            except Exception:
                return body
        from urllib.parse import parse_qsl, urlencode
        return urlencode([(k, fieldmap.get(k, v))
                          for k, v in parse_qsl(body, keep_blank_values=True)])
    if isinstance(body, dict):
        body = json.loads(json.dumps(body))     # deep copy so we don't mutate FLOW_STEPS
        _set(body)
    return body


def _apply_row(body, row):
    """Parameterize: overwrite recorded fields with THIS user's CSV row values via
    _PARAM_MAP (recorded field name -> CSV column). Any platform, any recording."""
    if not (_PARAM_MAP and row):
        return body
    fieldmap = {ff: row[col] for ff, col in _PARAM_MAP.items()
                if str(row.get(col, "")).strip() != ""}
    return _set_fields(body, fieldmap)


_ADDL_PLACEHOLDER = re.compile(
    r"\{\{\s*([A-Za-z0-9_]+)\s*(?:\|([^{}]*?))?\s*\}\}")


def _row_payment_method(row):
    """The payment method THIS row pays with.

    _FORCED_PAYMENT is normally a literal code and every user pays that way. It
    may instead name a CSV column -- {{payment_method}} -- so one pool can mix
    card payers with net-terms payers. That is not a convenience: on a B2B store
    most real orders are placed on account, so a pool where every user pays by
    card measures a population the site does not have.

    Returns "" when the row has no value and no default, which leaves the script
    in its normal dynamic-selection mode. That is not a silent card-to-offline
    swap -- a row with a blank method never declared a card in the first place --
    but it IS a data gap, so it is recorded in the call log.
    """
    tmpl = _FORCED_PAYMENT or ""
    if "{{" not in tmpl:
        return tmpl
    missing = []

    def _sub(m):
        col, fallback = m.group(1), m.group(2)
        val = (row or {}).get(col)
        val = "" if val is None else str(val).strip()
        if not val and fallback is not None:
            val = fallback.strip()
        if not val:
            missing.append(col)
        return val

    out = _ADDL_PLACEHOLDER.sub(_sub, tmpl).strip()
    if missing:
        _clog_annotate("payment method not set for this row (%s empty) - falling "
                       "back to dynamic selection" % ", ".join(sorted(set(missing))))
        return ""
    return out


def _method_is_card(method):
    """Does this method code mean a card gateway? Substring match against the
    KB's hosted-gateway list, the same test the rest of the script uses."""
    m = (method or "").strip().lower()
    return bool(m) and any(g in m for g in _HOSTED_GATEWAYS)


def _payment_intent_unmet(reason, method=None):
    """Record that a run DECLARED a card payment and could not honour it.

    Mirrors the browser track's fail-closed contract (browser_runner_gen._pay):
    a card that was asked for and did not happen fails the run. It is never
    quietly replaced with an offline method.

    The reason this matters is not tidiness. A run that was meant to exercise the
    card path, silently exercises the invoice path, and reports a pass is the
    same class of defect as an order that completes at price 0 and reports a
    pass: the HTTP calls all succeeded, and the thing under test never ran.
    Intent is DECLARED in the test data; it is never inferred from a blank cell.
    """
    _FLOW["payment_required"] = True
    _FLOW["payment_ok"] = False
    if not _FLOW.get("payment_err"):
        _FLOW["payment_err"] = reason
    _FLOW["gateway"] = method or _FLOW.get("gateway") or ""
    _flush()


def _row_addl(row):
    """Resolve {{csv_column}} placeholders in the payment additional_data against
    THIS user's CSV row.

    A stored-card token belongs to exactly ONE customer, so a run driving several
    accounts needs one token per account, not one for the whole run. Writing

        {"card_id": "{{payment_token}}", "cc_cid": "{{card_cvv}}", "save": false}

    lets every VU pick up the token from its own row. Any entry whose placeholder
    has no value in this row is DROPPED rather than sent empty -- an empty token
    is rejected by the gateway anyway, and dropping it lets the run fall back to
    the offline method and say so, instead of failing at payment.

    A placeholder may carry a default after a pipe -- {{card_cvv|123}} -- used
    only when the column is absent or blank for this row. That is for values
    that are genuinely constant across the whole pool (a sandbox CVV is the
    usual one) and saves repeating them on every row. It deliberately does NOT
    weaken the fail-closed contract: a placeholder with no default and no value
    still stops the run rather than paying by some other means.

    Without placeholders this returns the dict unchanged, so a single shared
    token keeps working exactly as before.
    """
    if not _PAYMENT_ADDL:
        return {}
    _method = _row_payment_method(row)
    if _ADDL_IS_PER_USER and not _method_is_card(_method):
        # This row pays by an offline method. The additional_data block was
        # authored for the card gateway, so none of it applies -- sending
        # fragments of it (a CVV with no card) would be meaningless. Offline
        # rows need no token, so this is NOT a dropped card payment.
        return {}
    out = {}
    _unresolved = []
    for k, v in _PAYMENT_ADDL.items():
        if not isinstance(v, str) or "{{" not in v:
            out[k] = v
            continue
        missing = []

        def _sub(m):
            col, fallback = m.group(1), m.group(2)
            val = (row or {}).get(col)
            val = "" if val is None else str(val).strip()
            if not val and fallback is not None:
                # {{col|literal}} -- the column wins when it has a value, so a
                # per-account override still beats the default.
                val = fallback.strip()
            if not val:
                missing.append(col)
            return val

        resolved = _ADDL_PLACEHOLDER.sub(_sub, v)
        if missing:
            _unresolved.append((k, sorted(set(missing))))
            continue
        out[k] = resolved
    if _unresolved and _ADDL_IS_PER_USER and _method_is_card(_method):
        cols = sorted({c for _k, cs in _unresolved for c in cs})
        _payment_intent_unmet(
            "test data declares a card payment (payment_method=%s) but this user's "
            "row has no value for %s, so no card could be presented. The run does "
            "NOT fall back to an offline method: that would report a pass for a "
            "card journey that never ran. Fill %s for this account, or declare the "
            "account as an offline payer."
            % (_method or "card gateway", ", ".join(cols), ", ".join(cols)),
            method=_method)
    return out


def _inject_payment(body, row=None):
    """Force the payment method + merge test-mode additional_data into a payment
    or place-order body (dict or JSON string). Returns the same type it received."""
    is_str = isinstance(body, str)
    data = body
    if is_str:
        if not body.strip():
            return body
        try:
            data = json.loads(body)
        except Exception:
            return body
    if not isinstance(data, dict):
        return body
    pm = data.get("paymentMethod")
    if not isinstance(pm, dict):
        pm = {}
        data["paymentMethod"] = pm
    _method = _row_payment_method(row)
    if _method:
        pm["method"] = _method
    if _PAYMENT_ADDL:
        _resolved = _row_addl(row)
        if _resolved:
            ad = pm.get("additional_data")
            if not isinstance(ad, dict):
                ad = {}
            ad.update(_resolved)
            pm["additional_data"] = ad
    return json.dumps(data) if is_str else data
# Common CAPTCHA response field names across platforms.
_CAPTCHA_FIELDS = ("g-recaptcha-response", "g_recaptcha_response", "h-captcha-response",
                   "recaptcha_response", "recaptcha", "captcha", "cf-turnstile-response",
                   "token")
# Markers that reveal a CAPTCHA challenge in a response body.
_CAPTCHA_MARKERS = ("g-recaptcha", "grecaptcha", "recaptcha/api", "www.google.com/recaptcha",
                    "h-captcha", "hcaptcha.com", "cf-turnstile", "challenges.cloudflare.com",
                    "please verify you are human", "invalid captcha", "captcha is required",
                    "captcha validation failed", "recaptcha validation failed")


def _has_captcha(txt):
    low = (txt or "").lower()
    return any(m in low for m in _CAPTCHA_MARKERS)


def _apply_captcha(body):
    """Inject the configured bypass/test token into a login/checkout body so a
    test-keyed or bypassed CAPTCHA env accepts the request. No-op when unset."""
    if not _CAPTCHA_TOKEN:
        return body
    if isinstance(body, dict):
        body = dict(body)
        placed = False
        for k in list(body):
            if k.lower() in _CAPTCHA_FIELDS:
                body[k] = _CAPTCHA_TOKEN
                placed = True
        if _CAPTCHA_FIELD:
            body[_CAPTCHA_FIELD] = _CAPTCHA_TOKEN
            placed = True
        if not placed:
            body["g-recaptcha-response"] = _CAPTCHA_TOKEN
    return body


def _write_stats():
    try:
        os.makedirs(os.path.dirname(_STATS_PATH), exist_ok=True)
        with open(_STATS_PATH, "w", encoding="utf-8") as fh:
            json.dump(_FLOW, fh)
    except Exception:
        pass


def _bump(key, n=1):
    with _LOCK:
        _FLOW[key] = _FLOW.get(key, 0) + n
        _write_stats()


# Assign a DISTINCT account to each virtual user (round-robin). This avoids
# concurrent users sharing one server-side cart/quote, which causes "no active
# cart" / "cart is locked" races and inconsistent order creation.
_USER_IDX = [0]
_ROW_IDX = [0]
# One number per virtual user, so a logged call can name who made it. Without
# it a stage can be counted but never turned into a funnel: calls do not tell
# you how many USERS got that far.
_VU_SEQ = [0]
# Data->thread sharing (JMeter-style CSV sharing mode):
#   "all_threads" (default) — one shared pool, round-robin with recycle (wrap) across
#                             ALL users; a row/account may be reused when users exceed
#                             the pool. Matches JMeter "All threads" + Recycle=true.
#   "unique"                — each user gets a DISTINCT row/account with NO reuse; once
#                             the pool is exhausted the extra users stop (StopUser), so
#                             a credential is never shared by two concurrent users.
_DATA_SHARING = __DATA_SHARING__

# EFFECTIVE PROFILE — the configuration this build ACTUALLY runs with, stamped into
# the stats so the report shows what ran (not the plan's proposed values). This is
# what makes a strict run auditable and comparable across releases.
_FLOW["effective_profile"] = {
    "users": "__USERS__", "duration": "__DUR__",
    "think_min": __TMIN__, "think_max": __TMAX__,
    "forced_payment": _FORCED_PAYMENT or "", "data_sharing": _DATA_SHARING,
    "faithful": _FAITHFUL, "strict": _STRICT, "build": _LTM_BUILD,
}


def _next_cred(creds):
    if not creds:
        return {}
    with _LOCK:
        i = _USER_IDX[0]
        _USER_IDX[0] += 1
    if _DATA_SHARING == "unique" and i >= len(creds):
        return None                      # pool exhausted -> caller stops this user
    return creds[i % len(creds)]


def _load_rows():
    """Every column of the testdata CSV as dict rows (for generic parameterization)."""
    rows = []
    try:
        with open(_DATA, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows.append({k: (v or "") for k, v in r.items() if k})
    except FileNotFoundError:
        pass
    return rows


def _next_row(rows):
    if not rows:
        return {}
    with _LOCK:
        i = _ROW_IDX[0]
        _ROW_IDX[0] += 1
    if _DATA_SHARING == "unique" and i >= len(rows):
        return None                      # pool exhausted -> caller stops this user
    return rows[i % len(rows)]


def _record_order(order_id, total=None):
    """Count ONE confirmed order, remember its id, and accumulate its VALUE so the
    report shows real revenue instead of £0. `total` is the cart's base_grand_total
    captured at checkout (None when unknown)."""
    with _LOCK:
        _FLOW["orders"] = _FLOW.get("orders", 0) + 1
        if order_id and str(order_id) != "confirmed":
            _FLOW.setdefault("order_ids", []).append(str(order_id))
        if total is not None:
            try:
                v = float(total)
                _FLOW["order_value_total"] = round(_FLOW.get("order_value_total", 0.0) + v, 2)
                _FLOW.setdefault("order_values", []).append(v)
            except (TypeError, ValueError):
                pass
        _write_stats()


# ---- checkout timeline instrumentation -----------------------------------
_CHECKOUT_LOG = []          # step-by-step record of the most recent checkout


def _clog_reset():
    with _LOCK:
        _CHECKOUT_LOG[:] = []


def _mask(obj):
    """Truncated request payload for logging, with sensitive values masked."""
    if obj is None:
        return ""
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj)
    except Exception:
        return str(obj)[:400]
    s = re.sub(r'("(?:password|cvv|cvn|card_?number|cc_?number|securitycode|'
              r'payment_token|token|authorization)"\s*:\s*")[^"]*',
              lambda m: m.group(1) + "***", s, flags=re.I)
    return s[:400]


# Stored-card tokens and card secrets must not persist into run artifacts. A
# vault token is not PAN data, but it is an operational identifier that can be
# used to charge someone's saved card on that store -- there is no reason to
# spread it through logs once the runtime binding has succeeded. The browser
# track has always redacted (browser_runner_gen._redact); this brings the HTTP
# track in line. Deliberately NARROW: sku, price, quote and order ids must
# survive, because the price-healing and business-data analysis read this log.
_SECRET_FIELD_RE = re.compile(
    r'("(?:card_id|public_hash|payment_token|paymentMethodNonce|'
    r'storedPaymentMethodId|cc_cid|cc_number|cvv|cvn|password)"\s*:\s*")[^"]*',
    re.I)
_LOG_PAN_RE = re.compile(r"(?:\d[ -]?){13,19}")


def _redact_secrets(s):
    """Strip card tokens, CVVs and PAN-shaped runs from anything written to disk."""
    if not s:
        return s
    s = _SECRET_FIELD_RE.sub(r"[REDACTED]", str(s))
    return _LOG_PAN_RE.sub("[REDACTED_PAN]", s)


def _clog(step, method, url, status, ms, body, ok, extra="", req=None):
    """Record one checkout step (url, request payload, status, elapsed ms,
    truncated response body) and surface the timeline live via ltm_flow.json."""
    entry = {"step": step, "method": method, "url": _redact_secrets(str(url)),
             "status": status,
             "ms": round(float(ms), 1), "ok": bool(ok),
             "req": _redact_secrets(req or ""),
             "body": _redact_secrets((body or "")[:300]), "extra": extra}
    with _LOCK:
        _CHECKOUT_LOG.append(entry)
        _FLOW["timeline"] = list(_CHECKOUT_LOG)
        _write_stats()


def _set_state(**kw):
    """Update the live Checkout State Report (cart id, item count, methods, state)."""
    with _LOCK:
        st = _FLOW.setdefault("checkout_state", {})
        st.update(kw)
        _write_stats()


def _clog_annotate(extra):
    """Attach an extra note (e.g. item count) to the most recent timeline entry."""
    with _LOCK:
        if _CHECKOUT_LOG:
            _CHECKOUT_LOG[-1]["extra"] = extra
            _FLOW["timeline"] = list(_CHECKOUT_LOG)
            _write_stats()


def _note_mode(m):
    """Record which checkout mode actually ran (faithful / rest-checkout /
    recorded-replay), so it's never ambiguous which path produced the results."""
    with _LOCK:
        _FLOW["mode"] = m
        _write_stats()


# ---- quote tracing --------------------------------------------------------
# Follow ONE quote/cart id through the whole checkout. If the id the token
# reports as its active cart differs from the id add-to-cart wrote to (or the id
# GET items reads back), the storefront session-quote and the token-quote have
# diverged — the classic cause of "empty cart" at shipping. This trace makes
# that divergence visible instead of leaving it to be inferred.
_QUOTE_TRACE = []

# Resolved-SKU cache: the CSV value is often a SEARCH TERM (manufacturer part
# number / name), NOT a sellable SKU. We search the catalog once per distinct
# term, resolve the real purchasable SKU, and cache it so we don't re-search on
# every iteration under load.
_SKU_CACHE = {}


def _sku_match_score(sku, term) -> int:
    """How closely a catalog result's SKU matches the requested term (LOWER is
    better). This is the fix for the wrong-product bug: catalog SEARCH ranks by
    relevance, so a fuzzy hit (e.g. a £41.99 item) can outrank the real product
    the term names. Ranking candidates by this score first makes the EXACT / near
    SKU win instead of the relevance guess. Alphanumeric-normalised, case-insensitive."""
    s = re.sub(r"[^a-z0-9]", "", str(sku or "").lower())
    t = re.sub(r"[^a-z0-9]", "", str(term or "").lower())
    if not s or not t:
        return 4
    if s == t:
        return 0                       # exact SKU match
    if s.startswith(t) or t.startswith(s) or s.endswith(t) or t.endswith(s):
        return 1                       # one is a prefix/suffix of the other
    if t in s or s in t:
        return 2                       # one contains the other
    return 3                           # relevance-only (weakest)


def _qtrace_reset():
    with _LOCK:
        _QUOTE_TRACE[:] = []


def _qtrace(stage, quote_id, note=""):
    with _LOCK:
        _QUOTE_TRACE.append({"stage": stage, "quote_id": str(quote_id or ""), "note": note})
        _FLOW.setdefault("checkout_state", {})["quote_trace"] = list(_QUOTE_TRACE)
        _write_stats()


def _line_price(item):
    """Numeric line price from a Magento quote-item dict; 0.0 when absent."""
    try:
        return float((item or {}).get("price") or 0)
    except Exception:
        return 0.0


def _extract_quote_id(body):
    """Pull a Magento quote/cart id from a carts/mine response. Handles a bare
    numeric id (POST /carts/mine), a quote object with 'id', or a cart item with
    'quote_id'. Returns a string id or ''."""
    if body is None:
        return ""
    s = str(body).strip().strip('"')
    if re.fullmatch(r"\d+", s):
        return s
    try:
        d = json.loads(body)
    except Exception:
        return ""
    if isinstance(d, list) and d:
        d = d[0]
    if isinstance(d, dict):
        for k in ("quote_id", "id", "cart_id", "entity_id"):
            v = d.get(k)
            if v not in (None, "", 0):
                return str(v)
    return ""


def _strip_query_param(url, key):
    """Remove a single query param from a URL (relative or absolute), preserving
    the rest. Used to drop a stale storefront form_key from REST bearer calls,
    where it is meaningless. Never raises — returns the original url on any error."""
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        p = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != key]
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
    except Exception:
        return url


def _mint_payment_token(client):
    """API-replay strategy: mint a FRESH sandbox payment token by replaying the
    provider's token-generation request (a PUBLIC sandbox test card is baked into
    the body at build time — never a real PAN). Returns the token string or None.
    Inert unless _PAY_API is configured (opt-in). Sandbox use only."""
    cfg = _PAY_API
    if not cfg:
        return None
    try:
        import json as _json, re as _re
        tr = cfg.get("token_request") or {}
        url = tr.get("url")
        if not url:
            return None
        body = tr.get("body")
        kw = {"json": body} if isinstance(body, dict) else {"data": body}
        with client.request(tr.get("method", "POST"), url,
                            name="Mint payment token (API)", catch_response=True, **kw) as r:
            ok = r.status_code < 400
            fld = cfg.get("token_field") or "id"
            tok = None
            if ok:
                try:
                    tok = (_json.loads(r.text or "{}") or {}).get(fld)
                except Exception:
                    tok = None
                if not tok:
                    m = _re.search(r'"%s"\s*:\s*"([^"]+)"' % _re.escape(fld), r.text or "")
                    tok = m.group(1) if m else None
            (r.success() if (ok and tok) else
             r.failure("token mint status=%s field=%s body=%s"
                       % (r.status_code, fld, (r.text or "")[:150])))
            return tok
    except Exception:
        return None


def _inject_pay_token(body, field, token):
    """Replace the value of `field` in a recorded consumer request body with the
    freshly minted token (JSON dict, JSON string, or form-encoded). Best-effort."""
    if not field or not token:
        return body
    try:
        import re as _re
        if isinstance(body, dict):
            def _walk(o):
                if isinstance(o, dict):
                    for k in list(o.keys()):
                        if k == field:
                            o[k] = token
                        else:
                            _walk(o[k])
                elif isinstance(o, list):
                    for it in o:
                        _walk(it)
            _walk(body)
            return body
        s = body if isinstance(body, str) else (str(body) if body else "")
        if not s:
            return body
        s = _re.sub(r'("%s"\s*:\s*")[^"]*(")' % _re.escape(field),
                    lambda m: m.group(1) + token + m.group(2), s)
        s = _re.sub(r'(%s=)[^&]*' % _re.escape(field), r'\g<1>' + token, s)
        return s
    except Exception:
        return body


def _clean_token(txt):
    """Return a valid Magento customer bearer token, or None.

    Magento's integration/customer/token returns the token as a BARE JSON string.
    Depending on config this is EITHER a ~32-char opaque token OR a JWT
    (header.payload.signature, base64url with '.', '_' and '-', well over 64
    chars). Accept both; reject only an HTML page or JSON error body (using those
    as a bearer causes 401 'consumer isn't authorized' on every carts/mine/*)."""
    t = (txt or "").strip().strip('"').strip()
    if not t or t[:1] in "<{[":              # HTML page / JSON error, not a token
        return None
    # opaque token or JWT: base64url alphabet plus dots, length >= 20
    if re.fullmatch(r"[A-Za-z0-9._-]{20,4096}", t):
        return t
    return None


# A JSON null or boolean is not an identifier. "increment_id": null in an
# address payload was read as an order id and reported as a placed order.
_NOT_AN_ID = {"", "0", "null", "none", "nil", "undefined", "false", "true"}


def _is_real_id(v) -> bool:
    return str(v or "").strip().lower() not in _NOT_AN_ID


# A page can carry a marker where a value will go. Magento writes
# /uenc/%25uenc%25/ into cart links for its own JavaScript to replace, and a
# correlation extractor cannot tell that from a real capture without looking.
_PLACEHOLDER_RE = re.compile(r"^(%25|%|\$\{|\{\{|__)[A-Za-z0-9_]+(%25|%|\}|\}\}|__)$")


def _is_placeholder(v) -> bool:
    v = str(v or "").strip()
    return bool(v) and bool(_PLACEHOLDER_RE.match(v))


def _extract_order_id(txt):
    """Pull a real order/quote id from a place-order or quote-submit response.

    Handles B2C orders AND B2B quote submission. A quote id is returned with a
    'Q' prefix so it is distinguishable from an order in the report. else None."""
    t = (txt or "").strip().strip('"')
    if t.isdigit() and t != "0":
        return t
    for pat in (r'"increment_id"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"order_number"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"orderNumber"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"order_id"\s*:\s*"?(\d+)',
                r'"entity_id"\s*:\s*"?(\d+)'):
        m = re.search(pat, t)
        if m and _is_real_id(m.group(1)):
            return m.group(1)
    # B2B negotiable-quote / RFQ submission returns a quote id / number.
    for pat in (r'"quote_id"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"quoteId"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"quote_number"\s*:\s*"?([A-Za-z0-9_-]+)',
                r'"rfq_number"\s*:\s*"?([A-Za-z0-9_-]+)'):
        m = re.search(pat, t)
        if m:
            return "Q" + m.group(1)
    return None


# Strong, platform-agnostic order/quote success URL signals (B2C + B2B). KB-driven
# (platform_rules.order_url_signals) with the standard set as defaults, so a
# crawl-observed / per-store success signal can EXTEND them without a code change.
_ORDER_URL_SIGNALS = __ORDER_URL_SIGNALS__
# Order/quote-placement request fragments across platforms (B2C orders with/without
# payment, and B2B quote submission) — likewise KB-driven with defaults.
_ORDER_PLACE_PATTERNS = __ORDER_PLACE_PATTERNS__
# Payment policy sourced from the Knowledge Base (single source of truth).
# Offline = no card token from a hosted iframe (B2B "order without payment" too);
# hosted = tokenizes in a 3rd-party iframe and cannot be HTTP-replayed.
_OFFLINE_PAYMENTS = __OFFLINE_PAYMENTS__
_HOSTED_GATEWAYS = __HOSTED_GATEWAYS__


def _confirm_order(url, status, txt):
    """Return an order id (or 'confirmed') if the response GENUINELY confirms an
    order, else None. Requires a real order id or a strong success URL/phrase —
    keyword-only matches are not counted, to avoid false positives."""
    if status is not None and status >= 400:
        return None
    low = (txt or "").lower()
    if '"error"' in low or "exception" in low or '"errors":true' in low:
        return None
    u = (url or "").lower()
    # Something other than the id has to say this is an order. Scraping an id
    # out of any response counted a billing-address popup as a placed order:
    # its payload carries "increment_id" and "entity_id" like an order does.
    confirms = (any(s in u for s in _ORDER_URL_SIGNALS)
                or "thank you for your order" in low
                or "your order number" in low
                # B2B quote submission (the order-without-payment path).
                or "quote has been submitted" in low or "quote request" in low
                or "your quote" in low or "quote submitted" in low)
    if not confirms:
        return None
    # Only now is an id in the body worth reading.
    return _extract_order_id(txt) or "confirmed"


# An application error carried inside a 200. Matched on JSON shapes rather than
# the word "error" anywhere, so a product description mentioning it is safe --
# and "error": false has to stay a pass.
_BODY_ERROR_RE = re.compile(
    r'"error"\s*:\s*true'
    r'|"isError"\s*:\s*true'
    r'|"success"\s*:\s*false'
    r'|"error_messages"\s*:\s*\[\s*[^\]\s]'
    r'|"errors"\s*:\s*\[\s*\{'
    # Salesforce Commerce (OCAPI) answers a refusal with a fault object.
    r'|"fault"\s*:\s*\{'
    # SOAP, still the wire format for a good deal of enterprise middleware.
    r'|<(?:\w+:)?Fault[\s>]'
    r'|<faultstring>', re.I)
_BODY_ERROR_MSG_RE = re.compile(
    r'"(?:error_messages|message|error|description)"\s*:\s*\[?\s*"([^"]{3,200})"'
    r'|<faultstring>([^<]{3,200})</faultstring>', re.I)


def _body_error(txt):
    """The store's own error message when a 2xx body says the request failed,
    else "". A refused add-to-cart answers 200 on this platform and several
    others; taking the status line at face value reported it as a success."""
    t = (txt or "")[:4000]
    if not t or not _BODY_ERROR_RE.search(t):
        return ""
    m = _BODY_ERROR_MSG_RE.search(t)
    if not m:
        return "the response body reports an error"
    return (m.group(1) or m.group(2) or "").strip() or \
        "the response body reports an error"


# Endpoints whose names CONTAIN an order-place pattern while doing something
# else. Magento's set-payment-information sets the method and returns true; the
# order is placed by payment-information, one word shorter.
_NOT_ORDER_PLACE = ("set-payment-information", "set-payment_information",
                    "setpaymentinformation", "payment-methods", "paymentmethods")


def _places_orders(path) -> bool:
    """Is this step the one that actually places the order?"""
    p = (path or "").lower()
    if any(k in p for k in _NOT_ORDER_PLACE):
        return False
    return any(k in p for k in _ORDER_PLACE_PATTERNS)


def _looks_like_order(path, status, txt):
    """Return an order id/'confirmed' if this step confirms an order, else None."""
    oid = _confirm_order(path, status, txt)
    if oid:
        return oid
    if status < 400 and _places_orders(path):
        return _extract_order_id(txt)   # only count when a real order id comes back
    return None


def _load_testdata():
    creds, keywords, products = [], [], []
    try:
        with open(_DATA, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("username"):
                    creds.append({"username": row["username"].strip(),
                                  "password": (row.get("password") or "").strip()})
                if row.get("search_keyword"):
                    keywords.append(row["search_keyword"].strip())
                if row.get("product_id"):
                    products.append(row["product_id"].strip())
    except FileNotFoundError:
        pass
    return creds, keywords, products


FLOW_STEPS = __FLOW__
# True when the recording contains the STOREFRONT add-to-cart controller. That
# path runs the store's own cart pricing (custom modules, per-customer/contract
# price); the REST /carts/mine/items endpoint bypasses it and can land the line
# at price 0. When present, the REST checkout reuses the quote it produced.
_STOREFRONT_CART_ADD = any("checkout/cart/add" in str(s.get("path") or "").lower()
                           for s in FLOW_STEPS)
# The recorded storefront cart-add step(s). The REST checkout validator replays
# these ITSELF, after the customer quote exists, so the priced line cannot land
# in a different quote than the one the order is placed from.
# The recorded group that contains the order placement, so the end-to-end
# transaction carries the name the recording used rather than a generic label.
_CHECKOUT_TXN = next(
    (str(s.get("group")) for s in reversed(FLOW_STEPS) if s.get("group")), "Checkout")
_SF_CART_ADD_STEPS = [s for s in FLOW_STEPS
                      if "checkout/cart/add" in str(s.get("path") or "").lower()]


class WebsiteUser(HttpUser):
    """Replays the recorded checkout flow as one correlated, asserted session."""

    # ---- business-transaction timing ------------------------------------
    # Every other timing here is ONE request. "How long does checkout take?"
    # spans eight of them, and until now could not be answered from the report.
    # The recording already groups its steps into journey stages (Home page,
    # Login, Search, PDP, Add to cart, Checkout) and carries that grouping into
    # this script as step["group"], so the boundaries are known -- nothing was
    # timing them.
    #
    # Each group is reported as its own sample named "TXN: <group>", alongside
    # the individual requests. Locust aggregates it like any other entry, so it
    # lands in the CSV, the percentile table and the SLA check for free.
    #
    # State is per user instance: several greenlets run this class at once, and
    # module-level state would interleave their timers.
    def _txn_begin(self, name):
        """Start timing a business transaction, closing any already open."""
        self._txn_end()
        if name:
            self._txn_name = str(name)
            self._txn_t0 = time.time()

    def _txn_end(self, failed_reason=None):
        """Close the open transaction and report it. Safe to call when none is
        open, so it can sit in a finally: and cover every early return."""
        name = getattr(self, "_txn_name", None)
        t0 = getattr(self, "_txn_t0", None)
        self._txn_name, self._txn_t0 = None, None
        if not name or t0 is None:
            return
        try:
            events.request.fire(
                request_type="TXN", name="TXN: %s" % name,
                response_time=(time.time() - t0) * 1000.0,
                response_length=0, context={},
                exception=Exception(failed_reason) if failed_reason else None)
        except Exception as _exc:      # never let reporting break a run
            _clog_annotate("transaction timing for %r not recorded: %s" % (name, _exc))
    # PACING. Think time alone makes throughput an OUTPUT of the run: as the
    # system slows, each user completes fewer iterations, so offered load falls
    # exactly when the system is under stress -- backwards for capacity work.
    #   between(a, b)          think time only; throughput drifts with latency
    #   constant_pacing(t)     each iteration takes at least t seconds in total,
    #                          so the rate holds until the app is slower than t
    #   constant_throughput(r) r iterations per second PER USER
    # __WAIT_TIME__ is chosen by the planner from the requested pacing.
    wait_time = __WAIT_TIME__

    def context(self):
        """Carried into every request event, so a logged call can say which
        user made it. Locust passes this through untouched; nothing else reads
        it, and adding it costs no request and no timing."""
        return {"vu": getattr(self, "_vu", 0)}

    def on_start(self):
        with _LOCK:
            _VU_SEQ[0] += 1
            self._vu = _VU_SEQ[0]
        self.credentials, self.search_keywords, self.product_ids = _load_testdata()
        self._rows = _load_rows()
        self._row = _next_row(self._rows)   # this user's full CSV row (all columns)
        if self._row is None:               # unique sharing: data pool exhausted
            from locust.exception import StopUser
            raise StopUser()
        self._vars = {}                     # correlation variables captured from responses
        self._form_key = None
        self._token = None
        self._address_id = None
        self._billing = None
        self._order_placed = False
        self._email = ""
        self._password = ""
        creds = _next_cred(self.credentials)   # distinct account per user (round-robin)
        if creds is None:                      # unique sharing: account pool exhausted
            from locust.exception import StopUser
            raise StopUser()
        self._email = creds.get("username", "")
        self._password = creds.get("password", "")
        # API-replay payment: mint a fresh sandbox token for this user (inert unless
        # _PAY_API is configured). Injected into the order request at replay time.
        self._payment_token = _mint_payment_token(self.client) if _PAY_API else None
        if _FAITHFUL:
            # Verbatim mode skips warm-up + login injection, BUT a REST /carts/mine/*
            # call still needs a LIVE customer bearer (a bearer cannot be replayed
            # verbatim — cookies alone give 401 'consumer isn't authorized to access
            # self'). So mint the token + capture the address even here whenever the
            # flow contains REST calls. Pure-HTML JMeter replays (_HAS_REST False)
            # stay fully verbatim: _ensure_rest_token is a no-op for them.
            if _HAS_REST and self._email:
                self._ensure_rest_token()
            return
        with self.client.get("/", name="GET / (warmup)", catch_response=True) as r:
            self._form_key = (self.client.cookies.get("form_key")
                              or self._extract_form_key(r.text))
            self._capture(r.text)                        # seed correlation vars
            if self._form_key:
                self._vars["form_key"] = self._form_key
            if r.status_code < 400:
                r.success()
            else:
                r.failure("warmup status %s" % r.status_code)
        # If the recorded/discovered flow captured no login request, establish an
        # authenticated storefront session up front (best-effort Magento endpoints).
        if self._email and not _HAS_LOGIN_STEP:
            payload = {"username": self._email, "email": self._email,
                       "login[username]": self._email, "password": self._password,
                       "login[password]": self._password, "form_key": self._form_key or ""}
            payload = _apply_captcha(payload)
            logged_in = False
            for lp in LOGIN_URLS:
                with self.client.post(lp, data=payload,
                                      headers={"X-Requested-With": "XMLHttpRequest"},
                                      name="Login", catch_response=True) as r:
                    low = (r.text or "").lower()
                    captcha = _has_captcha(low) and not _CAPTCHA_TOKEN
                    ok = r.status_code < 400 and not captcha and not any(
                        s in low for s in ('"errors":true', "invalid login",
                                           "invalid email", "incorrect", "could not"))
                    if ok:
                        logged_in = True
                        r.success()
                    elif captcha:
                        _bump("captcha")
                        r.failure("[Login] CAPTCHA challenge detected — disable CAPTCHA on "
                                  "the test env, use provider test keys, or set a bypass token")
                    else:
                        r.failure("[Login] %s at %s" % (r.status_code, lp))
                if logged_in:
                    break
            _bump("login_ok" if logged_in else "login_fail")
        # Magento REST endpoints (carts/mine, checkout) need a customer bearer token
        self._ensure_rest_token()

    def _ensure_rest_token(self):
        """Mint the REST customer bearer + capture the customer's default address,
        used to authorize /carts/mine/* calls. Idempotent and safe to call in
        faithful mode too: no-op without _HAS_REST / email, or once a token exists."""
        if _HAS_REST and self._email and not self._token:
            with self.client.post(_REST_PREFIX + "/integration/customer/token",
                                  json={"username": self._email, "password": self._password},
                                  name="REST customer token", catch_response=True) as r:
                tok = _clean_token(r.text)
                if r.status_code < 400 and tok:
                    self._token = tok
                    r.success()
                else:
                    # 200 with an HTML/garbage body means the token endpoint is not a
                    # clean API here; don't use a bad bearer (it causes 401 on mine/*).
                    r.failure("token status %s body=%s" % (r.status_code, (r.text or "")[:150]))
        # capture the logged-in customer's default shipping address id, so the
        # REST checkout calls use THIS user's address instead of the recorded one
        if self._token:
            with self.client.get(_REST_PREFIX + "/customers/me",
                                 headers={"Authorization": "Bearer %s" % self._token},
                                 name="REST customer profile", catch_response=True) as r:
                if r.status_code < 400:
                    try:
                        me = json.loads(r.text or "{}")
                        addrs = me.get("addresses") or []
                        dsid = me.get("default_shipping")
                        dbid = me.get("default_billing")
                        pick = next((a for a in addrs if str(a.get("id")) == str(dsid)), None) \
                            or (addrs[0] if addrs else None)
                        if pick:
                            self._address_id = pick.get("id")
                        bpick = next((a for a in addrs if str(a.get("id")) == str(dbid)), None) or pick
                        if bpick:
                            reg = bpick.get("region") or {}
                            self._billing = {
                                "firstname": bpick.get("firstname"),
                                "lastname": bpick.get("lastname"),
                                "street": bpick.get("street") or [],
                                "city": bpick.get("city"),
                                "country_id": bpick.get("country_id"),
                                "postcode": bpick.get("postcode"),
                                "telephone": bpick.get("telephone"),
                                "region": reg.get("region"),
                                "region_id": reg.get("region_id"),
                                "region_code": reg.get("region_code"),
                            }
                    except Exception:
                        pass
                    r.success()
                else:
                    r.failure("customer profile %s" % r.status_code)

    # recorded REST-checkout POSTs we replace with a clean API sequence
    # Recorded checkout calls the REST validator replaces with its own sequence.
    # "carts/mine/totals" is separate from "carts/mine/totals-information" and
    # was missing: replayed in recorded order it runs before the validator has
    # a cart, and answers "Current customer does not have an active cart".
    _REST_CHECKOUT_STEPS = ("carts/mine/shipping-information", "estimate-shipping",
                            "carts/mine/totals", "set-payment-information",
                            "carts/mine/payment-information")

__BROWSE_TASKS__
    @task(__CHECKOUT_W__)
    def checkout(self):
        # Stop BEFORE starting another iteration, not after finishing one: a user
        # that has already placed its orders should not add another basket to the
        # store just to be told to stop.
        if _MAX_ORDERS_PER_USER and getattr(self, "_orders_done", 0) >= _MAX_ORDERS_PER_USER:
            from locust.exception import StopUser
            _clog_annotate("this user placed its %d order(s) — stopping"
                           % _MAX_ORDERS_PER_USER)
            raise StopUser()
        self._order_placed = False
        self._txn_name, self._txn_t0 = None, None
        # Decide, once per iteration, which business groups run this time
        # (JMeter Throughput-Controller "Percent Executions"). None => no gating.
        active_groups = _roll_groups()
        # Faithful (JMeter-style): replay every recorded step verbatim, in order,
        # in one cookie-managed session — no parameterization, correlation, token
        # mint, or heal. Mirrors how JMeter runs the raw recording.
        if _FAITHFUL:
            _note_mode("faithful-replay")
            for step in FLOW_STEPS:
                if not _grp_active(step, active_groups):
                    continue
                self._run_raw(step)
            return
        # On a Magento REST target ALWAYS drive the order via the fail-fast
        # validator — never replay the recorded checkout POSTs (that produced the
        # estimate->totals->shipping->payment cascade of 400s). A missing token is
        # a clean STOP at Login, not a cascade.
        rest_order = _HAS_REST and not _RECORDED_CHECKOUT
        _note_mode("rest-checkout (validator)" if rest_order else "recorded-replay")
        for step in FLOW_STEPS:
            p = (step.get("path") or "").lower()
            # For a Magento REST target, SKIP the fragile recorded checkout POSTs —
            # we place the order via the robust API sequence below instead.
            if rest_order and any(k in p for k in self._REST_CHECKOUT_STEPS):
                continue
            # The storefront cart-add is what PRICES the line on a store that
            # applies price in a custom cart module. Running it HERE is a race:
            # the validator creates/loads the customer quote afterwards, so the
            # priced item can land in a different quote than the order is placed
            # from — which prices some orders and not others. The validator
            # replays it itself, once the quote exists.
            if rest_order and _SF_CART_ADD_STEPS and "checkout/cart/add" in p:
                continue
            if not _grp_active(step, active_groups):
                continue
            # A change of recorded group is a transaction boundary.
            _g = step.get("group") or ""
            if _g and _g != getattr(self, "_txn_name", None):
                self._txn_begin(_g)
            self._run_step(step)
        # The checkout group is a single business step even though it is served
        # by two mechanisms: the recorded steps replayed in the loop above, and
        # the API-driven validator below. Ending the transaction here and
        # reopening it would report one step as two samples -- and a live run
        # showed exactly that, the second lasting a fraction of a millisecond.
        # So the transaction is only closed when the validator is NOT about to
        # continue the same work.
        _continues = rest_order and getattr(self, "_txn_name", None) == _CHECKOUT_TXN
        if not _continues:
            self._txn_end()
        if rest_order:
            if not _continues:
                self._txn_begin(_CHECKOUT_TXN)
            try:
                self._rest_checkout()       # robust, API-driven order
                # The validator can come up short and the recorded order step
                # still succeed. That is a checkout that WORKED, so it has to
                # run while the transaction is still open. Closing first
                # reported "order not placed" against an order the very next
                # call went on to place, and left that order out of this user's
                # own count -- so a run capped at one order placed more.
                if not self._order_placed:
                    self._order_fallback()
            finally:
                self._txn_end(None if self._order_placed else "order not placed")
        elif not self._order_placed:
            self._order_fallback()
        if self._order_placed:
            self._orders_done = getattr(self, "_orders_done", 0) + 1

    def _order_fallback(self):
        """Place the order by the recorded step when the primary path did not.

        Skipped when there is nothing to place an order against. The checkout
        stopped before the cart held anything, so trying anyway produces a
        second and third failure -- "firstname is required" from an address
        that was never read -- and buries the reason it really stopped.
        """
        _st = (_FLOW.get("checkout_state") or {})
        if _st.get("stopped_at") and not _st.get("item_count"):
            _clog_annotate("no cart to order from (stopped at %s: %s) — "
                           "not attempting an order"
                           % (_st.get("stopped_at"),
                              _st.get("stop_reason") or "no reason given"))
            return
        # The recorded order step belongs to a group that the loop already
        # timed. Wrapping it again would double-count that group.
        self._place_order()

    def _run_raw(self, step):
        """Verbatim replay of one recorded step (JMeter-style): recorded body +
        recorded headers, cookies via the session, no mutation, no heal.

        Guardrail: a Magento REST /carts/mine/* (or /customers/me) call cannot be
        replayed verbatim — it needs a LIVE customer bearer. The recorded
        Authorization is single-use/stale and cookies alone give 401 'consumer
        isn't authorized to access self'. So on those calls we override with the
        freshly-minted token and drop the meaningless recorded form_key query
        param. Every other step stays byte-for-byte verbatim."""
        body = step.get("body")
        hdrs = dict(step.get("hdrs") or {})
        path = step["path"]
        _pl = str(path).lower()
        # API-replay: inject the freshly minted token into the discovered consumer
        # request (the merchant field the Payment Analyzer correlated it to).
        if _PAY_API and getattr(self, "_payment_token", None):
            _ce = str(_PAY_API.get("consumer_endpoint") or "").lower().split("?")[0]
            if _ce and _ce[-30:] in _pl:
                body = _inject_pay_token(body, _PAY_API.get("inject_field"), self._payment_token)
        if ("carts/mine" in _pl or "/customers/me" in _pl) and self._token:
            hdrs["Authorization"] = "Bearer %s" % self._token
            path = _strip_query_param(path, "form_key")
        ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "").lower()
        req = {}
        if "application/json" in ctype or (isinstance(body, str) and body.strip()[:1] in "{["):
            if isinstance(body, dict):
                req["json"] = body
            else:
                hdrs.setdefault("Content-Type", "application/json")
                req["data"] = body
        else:
            req["data"] = body if isinstance(body, dict) else (body or None)
        with self.client.request(step["method"], path, name=step["name"],
                                 headers=hdrs or None, catch_response=True, **req) as r:
            txt = r.text or ""
            ok = r.status_code < 400
            _berr = _body_error(txt) if ok else ""
            if _berr:
                ok = False
            for a in step.get("asserts", []):
                if a and a not in txt:
                    ok = False
                    break
            oid = (_looks_like_order(path, r.status_code, txt)
                   or _confirm_order(r.url, r.status_code, txt))
            if oid:
                _record_order(oid)
                self._order_placed = True
            if step.get("login"):
                _bump("login_ok" if ok else "login_fail")
            r.success() if ok else r.failure(
                "[%s] code=%s url=%s body=%s" % (step["name"], r.status_code, r.url, txt[:800]))

    def _rc(self, step, method, path, extra="", soft=False, **kw):
        """One instrumented checkout API call: log url + status + elapsed ms +
        truncated body (+ optional note) to the timeline, mark success/failure.
        Returns (ok, status, body).

        soft=True: an EXPECTED probe/substitution attempt (e.g. trying a candidate
        SKU that may be the wrong variant). On failure it is NOT counted as a load
        failure — the caller decides the real outcome and records ONE failure via
        _count_failure only when the whole step terminally fails. This stops
        substituted-past attempts from polluting the stats / RCA ("most failures")."""
        name = kw.pop("name", step)
        reqp = kw.get("json") if "json" in kw else kw.get("data")
        t0 = time.time()
        with self.client.request(method, path, name=name, catch_response=True, **kw) as r:
            ms = (time.time() - t0) * 1000.0
            body = r.text or ""
            ok = r.status_code < 400
            _clog(step, method, r.url, r.status_code, ms, body, ok,
                  extra=extra, req=_mask(reqp))
            if ok or soft:
                r.success()   # soft: expected probe — not a load failure
            else:
                r.failure("[%s] code=%s body=%s" % (step, r.status_code, body[:300]))
            return ok, r.status_code, body

    def _count_failure(self, name, reason, method="POST"):
        """Record ONE real Locust failure for a checkout transaction whose per-
        attempt requests were soft (substitution probes) — so a TERMINAL failure is
        still counted accurately, without the intermediate probes inflating it."""
        try:
            self.environment.events.request.fire(
                request_type=method, name=name, response_time=0,
                response_length=0, exception=RuntimeError(str(reason)[:200]),
                context={})
        except Exception:
            pass

    @staticmethod
    def _reason(body):
        """Human-readable failure reason from a Magento error body."""
        try:
            j = json.loads(body or "")
            if isinstance(j, dict) and j.get("message"):
                msg = str(j["message"])
                for k, v in (j.get("parameters") or {}).items():
                    msg = msg.replace("%" + str(k), str(v))
                return msg
        except Exception:
            pass
        return (body or "")[:200] or "no response body"

    def _stop(self, step, status, reason):
        """Record a hard STOP at the first failed checkout step and halt this
        transaction. If checkout is definitively broken (repeated stops, no
        orders), abort the WHOLE run rather than hammer a broken checkout."""
        _set_state(stopped_at=step, stop_reason=reason, stop_status=status)
        _clog("STOP", "", "", status, 0, reason, False,
              extra="Stopped at '%s' (status %s): %s" % (step, status, reason))
        with _LOCK:
            _FLOW["checkout_stops"] = _FLOW.get("checkout_stops", 0) + 1
            n_stops = _FLOW["checkout_stops"]
            orders = _FLOW.get("orders", 0)
            _write_stats()
        # TERMINAL failures won't fix themselves by retrying — a re-run just repeats
        # the same error (and re-adds to cart, re-hitting "qty not available"). Abort
        # the WHOLE run on the FIRST such failure, not after _ABORT_AFTER tries.
        _low = (reason or "").lower()
        _terminal = any(s in _low for s in (
            "out of stock", "not purchasable", "requested qty is not available",
            "terms and conditions", "agreement", "payment method is not available",
            "only hosted-gateway", "no payment methods", "no usable payment",
            "no shipping methods", "could not find a product", "no purchasable",
            "no rest customer token", "no sku", "does not match any route",
            "quote mismatch"))
        _abort = orders == 0 and (_terminal or (_ABORT_AFTER and n_stops >= _ABORT_AFTER))
        if _abort:
            print("[LT Metrics] Aborting run — %s: [%s] %s"
                  % ("terminal checkout failure" if _terminal
                     else "checkout broken (%d stops, 0 orders)" % n_stops, step, reason))
            _set_state(aborted=True, abort_reason="%s: %s" % (step, reason))
            try:
                self.environment.runner.quit()   # stop the whole Locust run
            except Exception:
                pass
        return False

    def _resolve_products(self, term):
        """Resolve an ORDERED LIST of candidate products for a data-driven value.

        THE DATA VALUE IS USUALLY THE SKU ITSELF — proven on Radwell: the value
        'c24071467' IS the product's sku (a catalog search for it returns exactly
        that sku). So the value is used as the FIRST candidate and added to the cart
        DIRECTLY (cart-add takes a sku), which gives the RIGHT product + its real
        price WITHOUT depending on search context. (Catalog search here runs in the
        LOGGED-IN customer session, whose customer-group results can differ from the
        product the value names — that mismatch is what produced the wrong £41.99
        product before.) Catalog SEARCH is kept only as a FALLBACK for when the value
        is a keyword (not a sku) and for out-of-stock substitution, ranked by how
        closely each result's sku matches the value. Cached per term.
        Each candidate: {sku, type_id, in_stock, item_options, price}."""
        term = str(term or "").strip()
        if not term:
            return []
        with _LOCK:
            if term in _SKU_CACHE:
                return list(_SKU_CACHE[term])
        cands, seen = [], set()

        def _add(c):
            s = c.get("sku")
            # key on (sku, has-options) so a configurable PARENT can appear BOTH as a
            # bare attempt AND as a properly-optioned add (the latter is what prices it).
            k = (s, bool(c.get("item_options")))
            if s and k not in seen:
                seen.add(k)
                cands.append(c)

        # 1) The value itself, tried DIRECTLY as a sku (in_stock=None -> unknown, so
        #    cart-add just tries it; a "not found / invalid sku" falls through to the
        #    search candidates below via the broadened substitution rule).
        _add({"sku": term, "type_id": None, "in_stock": None,
              "item_options": None, "price": None})
        # 2) Catalog SEARCH fallback, ranked by sku-match closeness then in-stock.
        search = self._graphql_search(term)
        search.sort(key=lambda c: (_sku_match_score(c.get("sku"), term),
                                   0 if c.get("in_stock") else 1))
        for c in search:
            _add(c)
        with _LOCK:
            _SKU_CACHE[term] = list(cands)
        return cands

    def _graphql_search(self, term):
        """Catalog search via GraphQL. Returns candidate products. Never raises.

        For a CONFIGURABLE product it returns a candidate that adds the PARENT sku
        with `configurable_item_options` for a chosen variant — preferring the NEW
        condition — because adding a bare child variant lands in the order at £0
        (the price is applied by the configurable + selected option, per customer).
        Simple products are returned as-is."""
        query = ("query($q:String!){products(search:$q,pageSize:10){items{"
                 "sku stock_status __typename "
                 "price_range{minimum_price{final_price{value}}} "
                 "... on ConfigurableProduct{"
                 "configurable_options{attribute_id attribute_code values{value_index label}} "
                 "variants{attributes{code value_index} product{sku stock_status "
                 "price_range{minimum_price{final_price{value}}}}}}}}}")
        headers = {"Content-Type": "application/json"}
        if _STORE:
            headers["Store"] = _STORE
        cands = []

        def _price(node):
            try:
                return ((node.get("price_range") or {}).get("minimum_price") or {}) \
                    .get("final_price", {}).get("value")
            except Exception:
                return None

        def _instock(v):
            return str((v.get("product") or {}).get("stock_status") or "").upper() == "IN_STOCK"
        try:
            with self.client.post(_GRAPHQL_URL, name="Resolve: graphql",
                                  json={"query": query, "variables": {"q": term}},
                                  headers=headers, catch_response=True) as r:
                ok = r.status_code < 400
                data = json.loads(r.text or "{}") if ok else {}
                items = (((data.get("data") or {}).get("products") or {}).get("items") or [])
                (r.success() if (ok and items)
                 else r.failure("graphql resolve status=%s items=%d" % (r.status_code, len(items))))
                for it in items:
                    if it.get("__typename") == "ConfigurableProduct":
                        c = self._configurable_candidate(it, _price, _instock)
                        if c:
                            cands.append(c)
                        continue
                    sku = it.get("sku")
                    if sku:
                        cands.append({"sku": str(sku), "type_id": it.get("__typename"),
                                      "in_stock": (str(it.get("stock_status") or "").upper()
                                                   == "IN_STOCK"),
                                      "item_options": None, "price": _price(it)})
        except Exception:
            cands = []
        return cands

    @staticmethod
    def _configurable_candidate(it, _price, _instock):
        """Build a PARENT-sku + configurable_item_options candidate for a
        ConfigurableProduct, choosing the variant to buy: prefer NEW condition and
        in-stock. Adding the parent WITH options is what makes Magento apply the
        real (per-customer) price — a bare child adds at £0. Returns None if it
        can't build options (caller then skips it)."""
        parent = it.get("sku")
        opts = it.get("configurable_options") or []
        variants = it.get("variants") or []
        if not (parent and opts and variants):
            return None
        # attribute_code -> attribute_id, and the value_index whose label is "NEW"
        code_to_attr, new_value_by_code = {}, {}
        for o in opts:
            code = o.get("attribute_code")
            code_to_attr[code] = o.get("attribute_id")
            for v in (o.get("values") or []):
                if str(v.get("label") or "").strip().upper() == "NEW":
                    new_value_by_code[code] = v.get("value_index")

        def _is_new(v):
            return any(new_value_by_code.get(a.get("code")) == a.get("value_index")
                       for a in (v.get("attributes") or []))
        chosen = (next((v for v in variants if _is_new(v) and _instock(v)), None)
                  or next((v for v in variants if _is_new(v)), None)
                  or next((v for v in variants if _instock(v)), None)
                  or variants[0])
        item_options = []
        for a in (chosen.get("attributes") or []):
            aid = code_to_attr.get(a.get("code"))
            if aid is not None and a.get("value_index") is not None:
                item_options.append({"option_id": str(aid),
                                     "option_value": int(a.get("value_index"))})
        if not item_options:
            return None
        return {"sku": str(parent), "type_id": "ConfigurableProduct",
                "in_stock": _instock(chosen), "item_options": item_options,
                "price": _price(chosen.get("product") or {}),
                "child_sku": (chosen.get("product") or {}).get("sku")}

    def _rest_checkout(self):
        """Checkout VALIDATOR: run the Magento REST order as a gated, fail-fast
        sequence. Each step is logged (url/status/ms/body); on the FIRST failure it
        STOPS and reports the exact step + reason — no cascade. Verifies the cart
        actually contains an item before shipping."""
        _clog_reset()
        _qtrace_reset()
        _set_state(state="START", cart_id="", item_count=0, shipping_methods=[],
                   payment_methods=[], stopped_at="", stop_reason="", order_id="",
                   quote_mismatch=False)
        auth = {"Authorization": "Bearer %s" % self._token}
        _qtrace("Customer token", "", "acquired (len=%d)" % len(self._token or ""))

        # Login (validated in on_start): a customer token is required for carts/mine.
        _clog("Login", "", "", 200 if self._token else 0, 0, "", bool(self._token),
              extra=("customer token acquired" if self._token else "no token"))
        if not self._token:
            return self._stop("Login", 0, "no REST customer token — cannot drive carts/mine")

        # Prefer an EXPLICIT sku from the data row (direct add, no search resolution).
        # For a configurable product (Radwell condition variants, etc.) this must be
        # the purchasable CHILD sku (in stock + priced) — a simple product that adds
        # directly, avoiding "You need to choose options" and out-of-stock parent
        # resolution. Falls back to search/product_id resolution when no sku given.
        _dsku = ((self._row.get("sku") if self._row else "") or "").strip()
        if _dsku:
            term = _dsku
            cands = [{"sku": _dsku, "type_id": None, "in_stock": None, "item_options": None}]
        else:
            # Resolve CANDIDATE products (in-stock first) from the data-driven value.
            # Substitution keeps the run going honestly: if a product is out of stock,
            # we try the NEXT in-stock search result — not a fake success.
            term = (str(random.choice(self.product_ids)) if self.product_ids
                    else (str(random.choice(self.search_keywords)) if self.search_keywords else ""))
            cands = self._resolve_products(term) if term else []
        if not cands and _REST_SKUS:
            cands = [{"sku": str(s), "type_id": None, "in_stock": None, "item_options": None}
                     for s in _REST_SKUS]
        _top = cands[0] if cands else {}
        _clog("Resolve SKU", "GET", _REST_PREFIX + "/products", 200 if cands else 404, 0, "",
              bool(cands),
              extra="term='%s' -> chosen sku='%s' price=%s (match=%s); %d candidate(s): %s"
              % (term, _top.get("sku", ""), _top.get("price"),
                 _sku_match_score(_top.get("sku"), term) if _top else "-",
                 len(cands), [c["sku"] for c in cands[:8]]))
        if not cands:
            return self._stop("Resolve SKU", 404,
                              "could not find any product for '%s' — catalog search returned "
                              "nothing; check the value or the store's search config" % term)

        # 1) Cart created (once — candidate products are added to this same quote)
        ok, st, body = self._rc("Cart created", "POST", _REST_PREFIX + _EP["cart"], headers=auth)
        if not ok:
            return self._stop("Cart created", st, self._reason(body))
        cart_id = _extract_quote_id(body) or (body or "").strip().strip('"')  # token's active quote
        _qtrace("POST /carts/mine (token active quote)", cart_id)
        _set_state(cart_id=cart_id, state="CART_CREATED")
        # 1b) START FROM AN EMPTY CART. A customer's quote persists between
        # iterations AND between runs, and every add stacks another unit onto it.
        # Once the running total exceeds available stock Magento rejects the order
        # with "The requested qty is not available" — and because the order never
        # completes, the quote is never cleared, so each attempt makes it worse.
        # Clearing first means every iteration buys exactly what the data says.
        _okc, _stc, _bc = self._rc("Cart contains items", "GET",
                                   _REST_PREFIX + _EP["items"], headers=auth,
                                   soft=True)
        try:
            _stale = json.loads(_bc or "[]")
        except Exception:
            _stale = []
        _stale = [i for i in _stale if isinstance(i, dict)] if isinstance(_stale, list) else []
        if _stale:
            for _it in _stale:
                _iid = _it.get("item_id")
                if _iid is None:
                    continue
                self._rc("Cart cleared", "DELETE",
                         "%s%s/%s" % (_REST_PREFIX, _EP["items"], _iid),
                         headers=auth, soft=True)
            _clog_annotate("cleared %d leftover cart line(s) (qty %s) before adding"
                           % (len(_stale), [i.get("qty") for i in _stale]))
        # 2) Items added — try candidates until one is actually purchasable. An
        # out-of-stock / "qty not available" response substitutes the NEXT product
        # (hybrid heal); a non-stock error is a real failure and stops at once.
        sku, qid_add, added, last_rsn = "", "", False, ""
        # 2a) PRICE-CORRECT PATH. Stores that apply the line price in a custom
        # STOREFRONT cart module never price a REST-added item — it lands at 0
        # and the order captures shipping/tax only. So replay the RECORDED
        # storefront cart-add HERE, now that POST /carts/mine has established
        # the customer's active quote, guaranteeing the priced line lands in the
        # quote this order is placed from. (Replaying it earlier, in the
        # FLOW_STEPS loop, races the quote and prices only some orders.)
        for _sfs in _SF_CART_ADD_STEPS:
            self._run_step(_sfs)
        # Then reuse that priced line instead of adding an unpriced one by REST.
        if _STOREFRONT_CART_ADD:
            _ok0, _st0, _b0 = self._rc("Cart contains items", "GET",
                                       _REST_PREFIX + _EP["items"], headers=auth,
                                       soft=True)
            try:
                _pre = json.loads(_b0 or "[]")
            except Exception:
                _pre = []
            _priced = ([i for i in _pre if _line_price(i) > 0]
                       if isinstance(_pre, list) else [])
            if _priced:
                sku, added = str(_priced[0].get("sku") or ""), True
                qid_add = _extract_quote_id(_b0) or cart_id
                _qtrace("storefront cart/add (priced)", qid_add,
                        "sku=%s price=%s" % (sku, _priced[0].get("price")))
                _clog_annotate(
                    "storefront add-to-cart priced this quote: sku=%s price=%s — "
                    "skipping the REST add (it would land at price 0)"
                    % (sku, _priced[0].get("price")))
        for _ci, _cand in enumerate([] if added else cands[:8]):
            _csku = _cand["sku"]
            cart_item = {"sku": _csku, "qty": _CART_QTY}
            if _cand.get("item_options"):
                cart_item["product_option"] = {
                    "extension_attributes": {"configurable_item_options": _cand["item_options"]}}
            ok, st, body = self._rc("Items added", "POST", _REST_PREFIX + _EP["items"],
                                    headers=auth, json={"cartItem": cart_item}, soft=True,
                                    extra="sku=%s qty=%s cart_id=%s (candidate %d/%d)"
                                    % (_csku, _CART_QTY, cart_id, _ci + 1, len(cands)))
            if ok:
                sku, added = _csku, True
                qid_add = _extract_quote_id(body)
                _qtrace("POST /carts/mine/items", qid_add, "sku=%s qty=%s" % (_csku, _CART_QTY))
                break
            last_rsn = self._reason(body)
            if any(s in last_rsn.lower() for s in (
                    # out-of-stock / not-purchasable -> try the next candidate
                    "requested qty is not available", "out of stock", "not available",
                    "in stock", "salable",
                    # value wasn't a valid sku (it's a keyword) -> fall through to the
                    # SEARCH candidates instead of stopping the whole run
                    "not found", "no such", "does not exist", "doesn't exist",
                    "could not be found", "invalid", "no such entity",
                    "requested product doesn't exist",
                    # configurable PARENT sku can't be added directly -> fall through
                    # to the resolved in-stock CHILD variant candidate
                    "choose options", "choose an option", "you need to choose",
                    "specify the product", "required option", "select ")):
                _clog_annotate("sku=%s not usable (%s) — substituting next candidate"
                               % (_csku, last_rsn))
                continue                      # try the next product/candidate
            self._count_failure("Items added", last_rsn)  # real, non-substitutable failure
            return self._stop("Items added", st,          # other error is terminal
                              "add-to-cart FAILED for sku='%s': %s" % (_csku, last_rsn))
        # LAST-RESORT in-stock fallback: every pinned / search candidate for this data
        # value was out of stock. Rather than abort the whole run on inventory (a
        # staging store constantly goes in/out of stock), discover ANY in-stock, priced
        # product from the catalog (GraphQL) and use it — the load test still exercises
        # the full cart -> checkout path. DISABLED under strict, which must fail
        # faithfully on the exact product the data specifies (inventory is real signal).
        if not added and not _STRICT:
            _tried = {str(c.get("sku")) for c in cands}
            _probe = [t for t in (getattr(self, "search_keywords", None) or []) if t][:2]
            _probe += ["the", "a", "1", "kit"]     # broad, high-recall catalog probes
            for _pt in _probe:
                if added:
                    break
                for _fc in (self._graphql_search(str(_pt)) or []):
                    _fsku = str(_fc.get("sku") or "")
                    if not _fc.get("in_stock") or not _fsku or _fsku in _tried:
                        continue
                    # ...and PRICED. The comment above this block already said
                    # "in-stock, priced"; only in_stock was ever checked. A
                    # substitute with no price sails in here, the cart-value gate
                    # then stops the run, and the report blames checkout for what
                    # is really a bad substitution. Seen live: the pinned sku was
                    # out of stock, this picked a 0-priced product, and the run
                    # died three steps later with no hint of why.
                    try:
                        _fprice = float(_fc.get("price") or _fc.get("final_price") or 0)
                    except (TypeError, ValueError):
                        _fprice = 0.0
                    if _fprice <= 0:
                        _clog_annotate("fallback candidate sku=%s skipped: in stock but "
                                       "priced %s — a 0-priced substitute makes the run "
                                       "meaningless" % (_fsku, _fprice))
                        continue
                    _tried.add(_fsku)
                    _fitem = {"sku": _fsku, "qty": _CART_QTY}
                    if _fc.get("item_options"):
                        _fitem["product_option"] = {"extension_attributes":
                            {"configurable_item_options": _fc["item_options"]}}
                    ok, st, body = self._rc("Items added", "POST", _REST_PREFIX + _EP["items"],
                        headers=auth, json={"cartItem": _fitem}, soft=True,
                        extra="in-stock fallback sku=%s (pinned product OOS)" % _fsku)
                    if ok:
                        sku, added = _fsku, True
                        qid_add = _extract_quote_id(body)
                        _clog_annotate("pinned product OOS — substituted discovered in-stock "
                                       "product sku=%s" % _fsku)
                        _qtrace("POST /carts/mine/items", qid_add, "sku=%s (fallback)" % _fsku)
                        break
                    last_rsn = self._reason(body)
        if not added:
            self._count_failure("Items added", last_rsn)  # all candidates exhausted
            return self._stop("Items added", st,
                              "no purchasable product for '%s' — all %d candidate(s) are out "
                              "of stock (last: %s)" % (term, len(cands), last_rsn))
        _set_state(state="ITEMS_ADDED")
        # 3) Cart contains items  (the critical verification — count must be > 0)
        ok, st, body = self._rc("Cart contains items", "GET",
                                _REST_PREFIX + _EP["items"], headers=auth)
        if not ok:
            return self._stop("Cart contains items", st, self._reason(body))
        try:
            items = json.loads(body or "[]")
        except Exception:
            items = []
        n_items = len(items) if isinstance(items, list) else 0
        qid_get = _extract_quote_id(body)      # quote the token READS back
        _qtrace("GET /carts/mine/items", qid_get, "items=%d" % n_items)
        # Quote drift = the smoking gun. Compare the ids we captured; ignore blanks.
        seen = [q for q in (cart_id, qid_add, qid_get) if q]
        mismatch = len(set(seen)) > 1
        _set_state(item_count=n_items, state="ITEMS_VERIFIED", quote_mismatch=mismatch)
        _clog_annotate("items in cart: %d (sku=%s) | quote create=%s add=%s get=%s%s"
                       % (n_items, sku, cart_id or "?", qid_add or "?", qid_get or "?",
                          "  ⚠ QUOTE MISMATCH" if mismatch else ""))
        if mismatch:
            return self._stop("Cart contains items", st,
                              "QUOTE MISMATCH — add-to-cart wrote to quote %s but the token's active "
                              "quote is %s (GET read %s). The storefront session-quote and the "
                              "token-quote have diverged; that is why the cart looks empty at "
                              "shipping. STOP before shipping/payment."
                              % (qid_add or "?", cart_id or "?", qid_get or "?"))
        if n_items < 1:
            return self._stop("Cart contains items", st,
                              "cart is EMPTY after add (0 items) — sku='%s' likely not found / "
                              "disabled / out-of-stock / wrong store view, or the add wrote to a "
                              "different quote (quote create=%s add=%s get=%s). STOP before "
                              "shipping/payment." % (sku, cart_id or "?", qid_add or "?", qid_get or "?"))
        # 3b) CART VALUE GATE. The cart has items — but are they worth anything?
        # A 0-priced line means the storefront pricing module did not run, and
        # placing the order anyway writes a worthless order to the store while
        # the report reads as a pass. Only enforced when the recording HAS a
        # storefront cart-add (i.e. the store is known to price that way), so
        # genuinely 0-priced catalogues are unaffected.
        if _SF_CART_ADD_STEPS:
            _orig_sku = str((self._row or {}).get("sku") or "").strip()
            _line_prices = ([_line_price(i) for i in items]
                            if isinstance(items, list) else [])
            if not any(p > 0 for p in _line_prices):
                return self._stop("Cart contains items", st,
                    "cart line price is 0 (sku='%s', quote=%s). %sPlacing this order "
                    "would create a 0-value order that captures shipping/tax only. "
                    "STOP before shipping/payment."
                    % (sku, cart_id or "?",
                       ("This is NOT the sku your data asked for -- the pinned product "
                        "was out of stock and this one was substituted. Pick an in-stock "
                        "product, or run in strict mode to fail on the real one. "
                        if sku != _orig_sku else
                        "The storefront pricing module did not apply. ")))
            _clog_annotate("cart value OK: %d item(s), line price(s)=%s"
                           % (n_items, [p for p in _line_prices]))
        # 4) Shipping methods available
        ok, st, body = self._rc("Shipping methods available", "POST",
            _REST_PREFIX + _EP["estimate_shipping"],
            headers=auth, json={"addressId": self._address_id})
        if not ok:
            return self._stop("Shipping methods available", st, self._reason(body))
        method_code = carrier_code = None
        try:
            ms = json.loads(body or "[]")
            pick = next((m for m in ms if m.get("available")), (ms[0] if ms else None))
            if pick:
                method_code, carrier_code = pick.get("method_code"), pick.get("carrier_code")
        except Exception:
            pass
        if not (method_code and carrier_code):
            return self._stop("Shipping methods available", st,
                              "no shipping methods returned for this cart/address")
        _set_state(shipping_methods=[str(method_code)], state="SHIPPING_AVAILABLE")
        # 5) Set shipping information
        addr = self._addr()
        ok, st, body = self._rc("Set shipping information", "POST",
            _REST_PREFIX + _EP["set_shipping"], headers=auth,
            json={"addressInformation": {"shipping_address": addr, "billing_address": addr,
                  "shipping_method_code": method_code, "shipping_carrier_code": carrier_code}})
        if not ok:
            return self._stop("Set shipping information", st, self._reason(body))
        _qtrace("Shipping set", cart_id, "method=%s/%s" % (carrier_code, method_code))
        # Capture the cart's REAL order value (base_grand_total) from the shipping
        # response — this is the same total Magento places the order at, so the
        # order is reported with its true value instead of £0.
        self._order_total = None
        try:
            _si = json.loads(body or "{}")
            _tot = (_si.get("totals") or {}) if isinstance(_si, dict) else {}
            _gt = _tot.get("base_grand_total", _tot.get("grand_total"))
            if _gt is not None:
                self._order_total = float(_gt)
        except Exception:
            self._order_total = None
        if self._order_total is not None:
            _set_state(state="SHIPPING_SELECTED", order_total=self._order_total)
        else:
            _set_state(state="SHIPPING_SELECTED")
        # 6) Payment methods available
        ok, st, body = self._rc("Payment methods available", "GET",
            _REST_PREFIX + _EP["payment_methods"], headers=auth)
        if not ok:
            return self._stop("Payment methods available", st, self._reason(body))
        codes, method = [], None
        try:
            ms = json.loads(body or "[]")
            codes = [m.get("code") for m in ms if m.get("code")]
            # 1) Prefer an OFFLINE / no-card-token method (KB vocabulary, first match).
            for pref in _OFFLINE_PAYMENTS:
                method = next((c for c in codes if pref in c.lower()), None)
                if method:
                    break
            # 2) Else the first code that isn't a recognised hosted card gateway.
            #    (Deliberately does NOT fall back to a hosted gateway: a hosted
            #     gateway tokenises the card in a 3rd-party iframe that HTTP replay
            #     cannot complete, so "placing" an order with it just fails.)
            if not method and codes:
                method = next((c for c in codes
                               if not any(g in c.lower() for g in _HOSTED_GATEWAYS)), None)
        except Exception:
            pass
        if not codes:
            return self._stop("Payment methods available", st,
                              "no payment methods available on the cart")
        _set_state(payment_methods=codes, state="PAYMENT_AVAILABLE")
        # Payment-method precedence (highest wins), every choice annotated so nothing
        # is silently overridden — this is what makes the run's method transparent:
        #   1. _FORCED_PAYMENT — explicit user/server override
        #   2. testdata.csv payment_method — the data the generator wrote, USED when it
        #      names a method actually enabled on THIS cart (and replayable). This is
        #      genuine consumption of the CSV column, not a dead field.
        #   3. Memory — a method a prior run on this target actually placed an order with
        #   4. offline-preference default computed above
        # STRICT: discard the offline-preference auto-pick — a reproducible run must
        # use ONLY an explicit or CSV-specified method, never a runtime guess.
        if _STRICT:
            method, _chosen_reason = "", "unset (strict: awaiting explicit/CSV method)"
        else:
            _chosen_reason = "offline-preference default"
        _mem_pay = _APPLIED_MEMORY.get("payment_method")
        if _mem_pay and _mem_pay in codes and not _STRICT:
            method, _chosen_reason = _mem_pay, "memory (a prior run placed an order with it)"
        _sub_note = ""      # "requested X -> used Y" note, folded into the final annotation
        _csv_pay = str((getattr(self, "_row", None) or {}).get("payment_method") or "").strip()
        if _csv_pay:
            _csv_hosted = any(g in _csv_pay.lower() for g in _HOSTED_GATEWAYS)
            if _csv_pay in codes and not (_csv_hosted and not getattr(self, "_payment_token", None)):
                method, _chosen_reason = _csv_pay, "testdata.csv payment_method column"
            else:
                _why = ("not enabled on this cart" if _csv_pay not in codes
                        else "a hosted card gateway that HTTP replay can't complete without a token")
                # HOLD this, don't annotate yet. _clog_annotate REPLACES the last
                # entry's note, so annotating here then again below silently threw
                # the substitution away — and "you asked for card, you got net
                # terms" is exactly the line a reader needs. Folded in below.
                _sub_note = ("testdata.csv requested payment_method='%s' but it is %s — "
                             "using '%s' (%s) instead"
                             % (_csv_pay, _why, method or "(none)", _chosen_reason))
        # Resolve it against THIS user's row. _FORCED_PAYMENT may name a column
        # -- {{payment_method}} -- and using it raw sends that literal to the
        # store, which answers "The requested Payment Method is not available"
        # for a method that IS on the cart. The token resolved and the method
        # did not, which is exactly what the failing run showed.
        _forced_now = _row_payment_method(self._row)
        if _forced_now:
            method, _chosen_reason = _forced_now, "explicit payment_method override"
        if _STRICT and not method:
            # Fail LOUDLY rather than auto-selecting — this is the whole point of strict.
            return self._stop("Payment methods available", st,
                "STRICT mode needs a deterministic payment method: set payment_method "
                "explicitly, or put a method that is enabled on the cart into testdata.csv. "
                "Requested CSV method=%r; cart offers %s." % (_csv_pay or None, codes))
        # API-replay: use the hosted gateway method WITH a per-VU minted token rather
        # than stopping — the token makes the hosted method HTTP-completable. (Disabled
        # under strict: a minted-token hosted method is still a runtime auto-choice.)
        if not method and _PAY_API and getattr(self, "_payment_token", None) and not _STRICT:
            method = (_PAY_API.get("payment_method")
                      or next((c for c in codes
                               if any(g in c.lower() for g in _HOSTED_GATEWAYS)), None))
            _chosen_reason = "API-replay hosted gateway + per-VU minted token"
        if not method:
            # Only hosted card gateways are enabled on this cart and none was
            # forced — HTTP replay cannot complete a hosted-iframe card payment.
            return self._stop("Payment methods available", st,
                              "only hosted card gateways available (%s) — none can be "
                              "HTTP-replayed under load. Enable an offline method (e.g. "
                              "purchaseorder / netterms / checkmo) on the cart, force one "
                              "with payment_method=<code>, or run the browser track "
                              "(--browser-payment) to drive the real card iframe." % codes)
        pm = {"method": method}
        # Transparency: record the method actually used + WHY, and the address source,
        # so the report never silently disagrees with the testdata.csv the user sees.
        _clog_annotate("payment method used: '%s' (%s); cart offered %s%s"
                       % (method, _chosen_reason, codes,
                          (" | " + _sub_note) if _sub_note else ""))
        _csv_country = str((getattr(self, "_row", None) or {}).get("country_id") or "").strip()
        _acct_country = str((self._billing or {}).get("country_id") or "").strip()
        if _csv_country and _acct_country and _csv_country != _acct_country:
            _clog_annotate("address used: the logged-in account's SAVED address "
                           "(country=%s, id=%s). testdata.csv address (country=%s) is not "
                           "applied to a logged-in REST checkout — Magento requires the "
                           "account's own saved address; the CSV address applies only when "
                           "the account is freshly registered from the CSV."
                           % (_acct_country, self._address_id, _csv_country))
        _addl = _row_addl(self._row)
        if _addl:
            pm["additional_data"] = _addl
        # API-replay: thread the freshly minted sandbox token into the REST payment
        # payload (paymentMethod.additional_data[<correlated field>]) so the order is
        # placed with a real per-VU token instead of an offline method. Inert unless
        # _PAY_API is configured and a token was minted.
        if _PAY_API and getattr(self, "_payment_token", None) and _PAY_API.get("inject_field"):
            pm.setdefault("additional_data", {})[_PAY_API["inject_field"]] = self._payment_token
        # Checkout agreements (terms & conditions). The order is rejected unless
        # active agreement ids are accepted. RECORDING-FIRST: the ids are stable
        # store config captured in the recorded place-order body, and the REST
        # agreements endpoints are often not exposed (404) — so prefer the recorded
        # ids and only hit REST as a fallback when the recording has none.
        _agr = self._agreement_ids()
        if _agr:
            pm["extension_attributes"] = {"agreement_ids": _agr}
            _clog_annotate("accepted agreement_ids=%s" % _agr)
        payload = {"paymentMethod": pm, "billingAddress": self._addr()}
        # 7) Set payment information. This is a totals PRE-FLIGHT that refreshes the
        # quote for the chosen method — the order itself is placed by the
        # payment-information call below. Magento staging quotes intermittently drop
        # the shipping-address assignment between shipping-information and this call
        # ("The shipping address is missing") even though shipping-information JUST
        # succeeded — the identical request succeeds on retry (observed: test116 200
        # vs test118 400, same account/address/method). So on that specific transient
        # error, re-bind the shipping address (re-POST shipping-information) and retry
        # once. If it still fails, do NOT hard-stop on an optional pre-flight — fall
        # through to place-order, which places the order and surfaces any real cause.
        ok, st, body = self._rc("Set payment information", "POST",
            _REST_PREFIX + _EP["set_payment"], headers=auth, json=payload)
        if not ok and not _STRICT and ("shipping address is missing" in (body or "").lower()
                                       or "set the address" in (body or "").lower()):
            _rebind = self._addr()
            self._rc("Re-bind shipping (heal)", "POST",
                _REST_PREFIX + _EP["set_shipping"], headers=auth,
                json={"addressInformation": {"shipping_address": _rebind,
                      "billing_address": _rebind, "shipping_method_code": method_code,
                      "shipping_carrier_code": carrier_code}})
            ok, st, body = self._rc("Set payment information (retry)", "POST",
                _REST_PREFIX + _EP["set_payment"], headers=auth, json=payload)
        if not ok and _STRICT:
            # STRICT: no self-heal, no best-effort continue — report the failure as-is
            # so the run is a faithful, reproducible measurement.
            return self._stop("Set payment information", st,
                              "%s (STRICT: self-heal disabled; tried method=%s, available=%s)"
                              % (self._reason(body), method, codes))
        if not ok:
            # Optional pre-flight failed: log it and continue to place-order rather
            # than failing the whole checkout. place-order carries the same payment
            # method + billing address, and (after the shipping re-bind above) will
            # place the order — or report the genuine failure at that step.
            _clog_annotate("set-payment-information failed (%s: %s) — continuing to "
                           "place-order (optional totals pre-flight)"
                           % (st, self._reason(body)))
        else:
            _set_state(state="PAYMENT_SELECTED")
        # 8) Order created (place order)
        ok, st, body = self._rc("Order created", "POST",
            _REST_PREFIX + _EP["place_order"], headers=auth, json=payload)
        # Same transient Magento flake as set-payment: the quote can drop its shipping
        # assignment between shipping-information and this FINAL place-order call
        # ("The shipping address is missing") even though shipping succeeded moments
        # earlier. Re-bind the shipping address and retry once. Disabled under strict
        # (a reproducible run must report the failure as-is, not self-correct).
        if not ok and not _STRICT and ("shipping address is missing" in (body or "").lower()
                                       or "set the address" in (body or "").lower()):
            _rebind = self._addr()
            self._rc("Re-bind shipping (heal)", "POST",
                _REST_PREFIX + _EP["set_shipping"], headers=auth,
                json={"addressInformation": {"shipping_address": _rebind,
                      "billing_address": _rebind, "shipping_method_code": method_code,
                      "shipping_carrier_code": carrier_code}})
            ok, st, body = self._rc("Order created (retry)", "POST",
                _REST_PREFIX + _EP["place_order"], headers=auth, json=payload)
        oid = _extract_order_id(body) if ok else None
        if not ok:
            return self._stop("Order created", st,
                              "%s (tried method=%s, available=%s)" % (self._reason(body), method, codes))
        if not oid:
            return self._stop("Order created", st, "no order id returned; body=" + body[:200])
        oid = self._order_number(oid)     # entity_id -> storefront-facing increment_id
        _record_order(oid, getattr(self, "_order_total", None))
        self._order_placed = True
        with _LOCK:                       # remember the method that actually worked
            _FLOW["winning_payment"] = method
            _write_stats()
        _qtrace("Order created", cart_id, "order_id=%s" % oid)
        _set_state(state="ORDER_CREATED", order_id=str(oid))
        _clog("Order ID", "", "", st, 0, "", True, extra="Order ID: %s" % oid)
        return True

    def _order_number(self, entity_id):
        """Return the order id to report.

        Magento's carts/mine/payment-information returns the internal ENTITY id, while
        the storefront 'My Orders' page and admin grid search by the INCREMENT id — so
        a raw entity id can look 'not found' in the storefront. It is tempting to
        resolve the increment id via GET /V1/orders/{id}, BUT that endpoint requires
        the admin permission Magento_Sales::actions_view: a CUSTOMER bearer token gets
        401 there. There is no customer-scoped REST endpoint to fetch an order by id,
        so a customer-token checkout can only surface the ENTITY id. We report it
        as-is (find it in Admin > Sales > Orders by ID) and deliberately do NOT call
        the admin endpoint, which would only add a noisy 401 to every order."""
        return entity_id

    def _ensure_cart(self, force=False):
        """Proactively add an item to the REST (carts/mine) cart before checkout.
        The recorded STOREFRONT add-to-cart writes to the session quote, but the
        token-authenticated REST checkout reads a DIFFERENT quote — which is why
        shipping-information reports an empty cart. Only runs when the flow does
        REST checkout and did not itself record a REST cart-add (or when forced)."""
        if not (_HAS_REST and self._token and (_NEEDS_REST_CART or force)):
            return
        sku = (str(random.choice(self.product_ids)) if self.product_ids
               else (str(random.choice(_REST_SKUS)) if _REST_SKUS
                     else (str(random.choice(self.search_keywords)) if self.search_keywords else None)))
        if not sku:
            return
        with self.client.post(_REST_PREFIX + "/carts/mine/items",
                              json={"cartItem": {"sku": sku, "qty": _CART_QTY}},
                              headers={"Authorization": "Bearer %s" % self._token},
                              name="Ensure cart item (REST)", catch_response=True) as r:
            if r.status_code < 400:
                r.success()
            else:
                # not fatal — the recorded flow may still populate the cart
                r.failure("ensure-cart %s %s" % (r.status_code, (r.text or "")[:150]))

    def _place_order(self):
        """Try platform strategies until an order is confirmed (any platform)."""
        # Strategy 1 — Magento REST place-order (customers with a bearer token).
        if self._token and self._rest_place_order():
            return
        # Strategy 2 — replay whatever order-placement request the recording captured
        # (works for Shopify / WooCommerce / SFCC / custom, driven by the recording).
        self._replay_order_step()

    def _addr(self):
        """Magento address dict (camelCase keys) from the logged-in customer's
        saved address. regionId is included ONLY when it is a positive integer —
        UK / no-numeric-region countries return a code (e.g. a NUTS value), which
        Magento rejects as regionId ('int expected'); there we omit it and rely on
        region (name) + regionCode + countryId. None values are dropped."""
        b = self._billing or {}
        addr = {"customerAddressId": self._address_id, "countryId": b.get("country_id"),
                "region": b.get("region"), "regionCode": b.get("region_code"),
                "street": b.get("street") or [], "city": b.get("city"),
                "postcode": b.get("postcode"), "firstname": b.get("firstname"),
                "lastname": b.get("lastname"), "telephone": b.get("telephone")}
        try:
            rid = int(b.get("region_id"))
            if rid > 0:
                addr["regionId"] = rid          # valid numeric region id only
        except (TypeError, ValueError):
            pass                                # non-int (e.g. NUTS code) -> omit
        return {k: v for k, v in addr.items() if v is not None}

    def _agreement_ids(self):
        """Checkout agreement (T&C) ids this store requires with an order.

        RECORDING-FIRST: the ids are stable store config captured in the
        recorded place-order body, and the REST agreements endpoints are
        frequently not exposed (404 on both, on two stores so far). Cached per
        user so a fallback lookup happens at most once.
        """
        _c = getattr(self, "_agr_cache", None)
        if _c is not None:
            return _c
        agr = [int(a) if str(a).isdigit() else a for a in _AGREEMENT_IDS]
        if agr:
            _clog("Checkout agreements", "", "", 200, 0, "", True,
                  extra="from recording: agreement_ids=%s" % agr)
        else:
            auth = ({"Authorization": "Bearer %s" % self._token}
                    if getattr(self, "_token", None) else None)
            for _ep in (_EP["agreements"], _EP["agreements_fallback"]):
                # A PROBE: asking whether this store exposes the endpoint at
                # all. Magento answers 404 on both when it does not, which is
                # the probe succeeding, not the run failing.
                _ok_a, _st_a, _body_a = self._rc("Checkout agreements", "GET",
                                                 _REST_PREFIX + _ep, headers=auth,
                                                 soft=True)
                if _ok_a:
                    try:
                        for _a in (json.loads(_body_a or "[]") or []):
                            _aid = _a.get("agreement_id", _a.get("agreementId"))
                            if _aid is not None and _a.get("is_active", True):
                                agr.append(int(_aid) if str(_aid).isdigit()
                                           else str(_aid))
                    except Exception:
                        pass
                if agr:
                    break
        self._agr_cache = agr
        return agr

    def _rest_place_order(self):
        auth = {"Authorization": "Bearer %s" % self._token}
        method, codes = None, []
        with self.client.get(_REST_PREFIX + "/carts/mine/payment-methods",
                             headers=auth, name="REST payment-methods",
                             catch_response=True) as r:
            try:
                ms = json.loads(r.text or "[]")
                codes = [m.get("code") for m in ms if m.get("code")]
                # Prefer OFFLINE / no-card methods. Hosted card gateways (CyberSource,
                # Adyen, Stripe, Braintree, etc.) tokenize the card in a 3rd-party
                # iframe that a pure-HTTP load test cannot complete, so we place the
                # order with an offline method when one is enabled on the cart.
                for pref in _OFFLINE_PAYMENTS:
                    method = next((c for c in codes if pref in c.lower()), None)
                    if method:
                        break
                if not method and codes:
                    # First non-hosted code. Do NOT fall back to a hosted gateway
                    # (codes[0]) — its card token can't be produced by HTTP replay,
                    # so an order placed with it just fails. Leaving method empty
                    # surfaces the real cause (no replayable payment method).
                    method = next((c for c in codes
                                   if not any(g in c.lower() for g in _HOSTED_GATEWAYS)),
                                  None)
            except Exception:
                pass
            r.success() if r.status_code < 400 else r.failure("pay-methods %s" % r.status_code)
        pm = {"method": _row_payment_method(self._row) or method or ""}
        _addl = _row_addl(self._row)
        if _addl:
            pm["additional_data"] = _addl   # test-mode / stored-card params, per user
        # API-replay: inject the minted sandbox token into additional_data (inert
        # unless _PAY_API is configured and a token was minted for this user).
        if _PAY_API and getattr(self, "_payment_token", None) and _PAY_API.get("inject_field"):
            pm.setdefault("additional_data", {})[_PAY_API["inject_field"]] = self._payment_token
        # The order is refused without the terms accepted, exactly as on the main
        # path. This one was missing it, and every attempt came back
        # "First, agree to the terms and conditions".
        _agr = self._agreement_ids()
        if _agr:
            pm["extension_attributes"] = {"agreement_ids": _agr}
        payload = {"paymentMethod": pm}
        if self._billing:
            payload["billingAddress"] = self._addr()      # camelCase — correct regionId
        with self.client.post(_REST_PREFIX + "/carts/mine/payment-information",
                             json=payload, headers=auth, name="Place Order (REST)",
                             catch_response=True) as r:
            body = (r.text or "").strip().strip('"')
            oid = _extract_order_id(body) if r.status_code < 400 else None
            if oid:
                oid = self._order_number(oid)   # entity_id -> storefront increment_id
                _record_order(oid)
                self._order_placed = True
                r.success()
                return True
            # surface the tried method + what the store ACTUALLY offers, so the
            # user knows exactly which offline code to set.
            r.failure("[Place Order REST] method=%s code=%s available=%s body=%s"
                      % (pm["method"], r.status_code, codes, body[:600]))
        return False

    def _replay_order_step(self):
        for s in FLOW_STEPS:
            if not _places_orders(s.get("path")):
                continue
            body = s.get("body")
            if isinstance(body, dict):
                body = dict(body)
                if self._form_key and "form_key" in body:
                    body["form_key"] = self._form_key
            if _FORCED_PAYMENT or _PAYMENT_ADDL:
                body = _inject_payment(body, self._row)   # per-user stored-card params
            if _PARAM_MAP and self._row:
                body = _apply_row(body, self._row)   # card/shipping/etc. from CSV
            _rpath = s["path"]
            _rpath, body = self._correlate(_rpath, body)   # inject captured vars
            headers = {}
            if s.get("xhr"):
                headers["X-Requested-With"] = "XMLHttpRequest"
            if s.get("rest") and self._token:
                headers["Authorization"] = "Bearer %s" % self._token
            if s.get("json") and isinstance(body, str) and body.strip():
                headers["Content-Type"] = "application/json"
                req = {"data": body}
            elif s.get("json"):
                req = {"json": body if isinstance(body, dict) else {}}
            else:
                req = {"data": body if isinstance(body, dict) else (body or None)}
            with self.client.request(s["method"], _rpath, name="Place Order (replay)",
                                     headers=headers or None, catch_response=True, **req) as r:
                self._capture(r.text or "")
                oid = _confirm_order(r.url, r.status_code, r.text or "")
                if oid:
                    _record_order(oid)
                    self._order_placed = True
                    r.success()
                    return True
                r.failure("[Place Order replay] %s code=%s" % (s.get("name"), r.status_code))
        return False

    def _capture(self, text):
        """JMeter-style extractor: pull correlation values out of a response into
        self._vars using the configured regexes (first match wins per variable).

        A match that is itself a template placeholder is skipped. Magento
        renders cart-add links with /uenc/%25uenc%25/ in them for its own
        JavaScript to fill in; capturing that and injecting it downstream sent
        uenc=%2525uenc%2525 and the store answered "Selected contract is not
        valid." The recorded value is kept instead, which is a real one.
        """
        if not (_CORRELATIONS and text):
            return
        for c in _CORRELATIONS:
            for pat in c.get("extract", []):
                try:
                    m = re.search(pat, text)
                except Exception:
                    continue
                if m and _is_placeholder(m.group(1)):
                    _clog_annotate("%s looked like a placeholder in the page "
                                   "(%s) — keeping the recorded value"
                                   % (c["name"], m.group(1)[:40]))
                    continue           # keep looking; a later pattern may be real
                if m:
                    self._vars[c["name"]] = m.group(1)
                    break

    def _correlate(self, path, body):
        """Inject captured correlation variables into this request's URL params,
        known path slots (uenc) and body fields."""
        if not (_CORRELATIONS and self._vars):
            return path, body
        from urllib.parse import quote_plus as _qp
        fieldmap = {}
        for c in _CORRELATIONS:
            val = self._vars.get(c["name"])
            if val is None or val == "":
                continue
            for f in c.get("inject", []):
                path = re.sub(r"([?&]" + re.escape(f) + r"=)[^&]*",
                              lambda m, v=val: m.group(1) + _qp(str(v)), path)
                fieldmap[f] = val
            if c["name"] == "uenc":
                path = re.sub(r"(/uenc/)[^/]+", lambda m, v=val: m.group(1) + str(v), path)
        if fieldmap:
            body = _set_fields(body, fieldmap)
        return path, body

    def _run_step(self, step, _healing=False):
        path = step["path"]
        body = step.get("body")
        if isinstance(body, dict):
            body = dict(body)
            if self._form_key and "form_key" in body:
                body["form_key"] = self._form_key
            if step.get("login"):
                for k in list(body):
                    lk = k.lower()
                    if self._email and ("email" in lk or "user" in lk or lk == "login"):
                        body[k] = self._email
                    if self._password and "pass" in lk:
                        body[k] = self._password
                body = _apply_captcha(body)
            elif _CAPTCHA_TOKEN and any(k in (path or "").lower()
                                        for k in _ORDER_PLACE_PATTERNS):
                body = _apply_captcha(body)
        # search using the test data: a search_keyword OR a product_id/name/any id
        terms = self.search_keywords or self.product_ids
        if terms and ("catalogsearch" in path or "q=" in path):
            from urllib.parse import quote_plus as _qp
            kw = _qp(str(random.choice(terms)))
            path = re.sub(r"([?&]q=)[^&]*", lambda m: m.group(1) + kw, path)
        # add-to-cart using product ids from the uploaded CSV. The STOREFRONT
        # controller addresses products by NUMERIC entity id, so substituting a
        # sku here (e.g. "c30700506") breaks the add SILENTLY: Magento cannot load
        # the product, the request still returns 200/302, nothing enters the quote,
        # and checkout falls back to an unpriced REST add. Only substitute a
        # numeric id; otherwise keep the recorded one, which is known to work.
        if self.product_ids and "checkout/cart/add" in path and "/product/" in path:
            _npids = [p for p in (str(x).strip() for x in self.product_ids)
                      if p.isdigit()]
            if _npids:
                pid = random.choice(_npids)
                path = re.sub(r"(/product/)\d+", lambda m: m.group(1) + pid, path)
        # cart quantity override: raise units per add-to-cart to increase load
        _low = (path or "").lower()
        if _CART_QTY > 1 and any(k in _low for k in _ADD_CART_SIGNALS):
            path = re.sub(r"([?&](?:qty|quantity)=)\d+",
                          lambda m: m.group(1) + str(_CART_QTY), path)
            if isinstance(body, dict):
                for bk in list(body):
                    if bk.lower() in ("qty", "quantity"):
                        body[bk] = _CART_QTY
                ci = body.get("cartItem")
                if isinstance(ci, dict) and "qty" in ci:
                    ci["qty"] = _CART_QTY
            elif isinstance(body, str) and body:
                body = re.sub(r'("(?:qty|quantity)"\s*:\s*)\d+',
                              lambda m: m.group(1) + str(_CART_QTY), body)
                body = re.sub(r'((?:^|&)(?:qty|quantity)=)\d+',
                              lambda m: m.group(1) + str(_CART_QTY), body)
        # inject THIS customer's shipping address id into REST checkout bodies
        if self._address_id and isinstance(body, str) and body:
            aid = str(self._address_id)
            for fld in ("addressId", "customer_address_id", "customerAddressId"):
                body = re.sub(r'("' + fld + r'"\s*:\s*)"?\d+"?',
                              lambda m, a=aid: m.group(1) + a, body)
        # gateway test-mode payment: force method + merge additional_data on the
        # payment / place-order call so a real (test) card order completes
        if (_FORCED_PAYMENT or _PAYMENT_ADDL) and any(
                k in (path or "").lower()
                for k in ("payment-information", "set-payment", "placeorder", "place-order")):
            body = _inject_payment(body, self._row)
        # generic parameterization: card / shipping / billing / contact / etc.
        if _PARAM_MAP and self._row:
            body = _apply_row(body, self._row)
        # correlation: inject values captured from earlier responses (form_key,
        # tokens, ids, dynamic iframe URLs, …) into this request
        path, body = self._correlate(path, body)
        headers = {}
        if step.get("xhr"):
            headers["X-Requested-With"] = "XMLHttpRequest"
        if step.get("rest") and self._token:
            headers["Authorization"] = "Bearer %s" % self._token
        if _EXTRA_HEADERS:
            headers.update(_EXTRA_HEADERS)   # AI self-repair proposed headers
        # treat as JSON if the recording said so, OR it's a GraphQL endpoint, OR the
        # body clearly looks like JSON — otherwise servers reject with 400 content-type
        _json = (step.get("json")
                 or (path or "").split("?")[0].rstrip("/").endswith("/graphql")
                 or (isinstance(body, str) and body.strip()[:1] in "{["))
        req = {}
        if _json:
            if isinstance(body, dict):
                req["json"] = body
            elif isinstance(body, str) and body.strip():
                headers["Content-Type"] = "application/json"
                req["data"] = body
            else:
                req["json"] = {}
        else:
            req["data"] = body if isinstance(body, dict) else (body or None)
        path = re.sub(r"^/{2,}", "/", path)          # collapse accidental leading //
        with self.client.request(step["method"], path, name=step["name"],
                                 headers=headers or None, catch_response=True, **req) as r:
            txt = r.text or ""
            self._capture(txt)                # harvest correlation vars from response
            ok = r.status_code < 400
            # A 200 whose body says the request was refused is not a success.
            _berr = _body_error(txt) if ok else ""
            if _berr:
                ok = False
                _clog_annotate("%s answered 200 but refused it: %s"
                               % (step.get("name") or path, _berr))
            for a in step.get("asserts", []):
                if a and a not in txt:
                    ok = False
                    break
            if step.get("login"):
                low = txt.lower()
                captcha = _has_captcha(low) and not _CAPTCHA_TOKEN
                bad = captcha or any(s in low for s in ('"errors":true', "invalid login",
                          "invalid email", "incorrect", "could not"))
                ok = ok and not bad
                _bump("login_ok" if ok else "login_fail")
                if captcha:
                    _bump("captcha")
                if ok:
                    r.success()
                elif captcha:
                    r.failure("[Login] CAPTCHA challenge detected — disable CAPTCHA on the "
                              "test env, use provider test keys, or set a bypass token")
                else:
                    r.failure("[Login] FAILED code=%s body=%s" % (r.status_code, txt[:800]))
                return
            _oid = (_looks_like_order(step["path"], r.status_code, txt)
                    or _confirm_order(r.url, r.status_code, txt))
            if _oid:
                _record_order(_oid)
                self._order_placed = True
            if ok:
                r.success()
                return
            if _has_captcha(txt) and not _CAPTCHA_TOKEN:
                _bump("captcha")
                r.failure("[%s] CAPTCHA challenge detected — disable CAPTCHA on the test "
                          "env, use provider test keys, or set a bypass token" % step["name"])
            else:
                r.failure("[%s] code=%s url=%s body=%s"
                          % (step["name"], r.status_code, r.url, txt[:800]))
            fail_status, fail_txt = r.status_code, txt
        # ---- auto-heal: recognize the failure, fix it in-run, and retry once ----
        if not _healing and self._heal(step, fail_status, fail_txt):
            _bump("heals")
            self._run_step(step, _healing=True)

    def _heal(self, step, status, txt):
        """Deterministic run-time self-healing for common checkout failures.

        Returns True if a remedy was applied so the caller retries the step once.
        Remedies are generic Magento REST / data-driven — nothing site-specific.
        """
        if _STRICT:
            # Reproducible mode: never self-heal. Log that a remedy was available but
            # deliberately not applied, and report the failure as-is.
            _clog_annotate("STRICT: self-heal suppressed for '%s' (status %s) — failure "
                           "reported as-is for a comparable measurement"
                           % ((step or {}).get("name", "?"), status))
            return False
        low = (txt or "").lower()
        # 1) REST checkout reports an empty cart -> add an item to the REST quote
        if self._token and ("empty cart" in low or "add an item to cart" in low
                            or "cart is empty" in low):
            sku = (random.choice(self.product_ids) if self.product_ids
                   else (random.choice(_REST_SKUS) if _REST_SKUS
                         else (random.choice(self.search_keywords) if self.search_keywords else None)))
            if not sku:
                return False
            with self.client.post(_REST_PREFIX + "/carts/mine/items",
                                  json={"cartItem": {"sku": str(sku), "qty": _CART_QTY}},
                                  headers={"Authorization": "Bearer %s" % self._token},
                                  name="AUTO-HEAL add cart item", catch_response=True) as hr:
                if hr.status_code < 400:
                    hr.success()
                    return True
                hr.failure("heal add-item %s body=%s" % (hr.status_code, (hr.text or "")[:120]))
            return False
        # 2) payment reports the shipping address missing -> run shipping-information first
        if "shipping address is missing" in low or "set the address" in low:
            for s in FLOW_STEPS:
                if "shipping-information" in (s.get("path") or ""):
                    self._run_step(dict(s), _healing=True)
                    return True
        # 3) token/consumer auth expired -> refresh the customer bearer token
        if status == 401 and self._email:
            with self.client.post(_REST_PREFIX + "/integration/customer/token",
                                  json={"username": self._email, "password": self._password},
                                  name="AUTO-HEAL refresh token", catch_response=True) as hr:
                tok = _clean_token(hr.text)
                if hr.status_code < 400 and tok:
                    self._token = tok
                    hr.success()
                    return True
                hr.failure("heal token %s" % hr.status_code)
        return False

    @staticmethod
    def _extract_form_key(html):
        m = re.search(r'name="form_key"[^>]*value="([^"]+)"', html or "")
        return m.group(1) if m else None


@events.request.add_listener
def _ltm_on_request(request_type=None, name=None, response_time=None,
                     response_length=None, response=None, context=None,
                     exception=None, start_time=None, url=None, **kw):
    """Stream EVERY request to results/ltm_calls.jsonl (one JSON per line) so the
    UI can show a live JMeter-style results tree with request/response/status.
    Bodies are truncated (~2KB) and secrets masked. Fully guarded — a logging
    error never affects the load test."""
    try:
        with _LOCK:
            if _CALL_SEQ[0] >= _CALLS_CAP:
                return
            seq = _CALL_SEQ[0]
            _CALL_SEQ[0] += 1
        status, req_body, resp_body = 0, None, ""
        rq = getattr(response, "request", None) if response is not None else None
        try:
            if response is not None:
                status = int(getattr(response, "status_code", 0) or 0)
                if rq is not None:
                    req_body = getattr(rq, "body", None)
                try:
                    resp_body = response.text
                except Exception:
                    resp_body = ""
        except Exception:
            pass
        ok = (exception is None) and (status == 0 or status < 400)
        if exception is not None:
            err = str(exception)[:300]
        elif not ok:
            err = "HTTP %s" % status
        else:
            err = ""
        entry = {
            "seq": seq,
            "ts": round(time.time(), 3),
            "group": _stage_of(name),
            "vu": (context or {}).get("vu", 0) if isinstance(context, dict) else 0,
            "name": name or "",
            "method": (request_type or getattr(rq, "method", "") or ""),
            "url": str(url or getattr(rq, "url", "") or ""),
            "status": status,
            "ok": bool(ok),
            "ms": round(float(response_time or 0), 1),
            "req": _redact(req_body),
            "resp": _redact(resp_body),
            "error": err,
        }
        line = json.dumps(entry)
        with _LOCK:
            with open(_CALLS_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


@events.quitting.add_listener
def _log_summary(environment, **kwargs):
    stats = environment.stats.total
    fr = stats.fail_ratio * 100 if stats.num_requests else 0
    print("\n[LT Metrics] Requests=%s Failures=%s (%.2f%%)"
          % (stats.num_requests, stats.num_failures, fr))
    print("[LT Metrics] Login success=%s failure=%s  Orders created=%s  Auto-heals=%s"
          % (_FLOW["login_ok"], _FLOW["login_fail"], _FLOW["orders"], _FLOW["heals"]))
    if _APPLIED_MEMORY:
        print("[LT Metrics] Applied from memory (no LLM): %s" % _APPLIED_MEMORY)
    if _FLOW_MODEL:
        _reached = (_FLOW.get("checkout_state") or {}).get("state", "")
        print("[LT Metrics] Business flow model (from KB): %s" % " -> ".join(_FLOW_MODEL))
        print("[LT Metrics] Reached state: %s" % (_reached or "-"))
    if _FLOW.get("captcha"):
        print("[LT Metrics] CAPTCHA challenges hit=%s — disable CAPTCHA on the test env, use "
              "provider test keys, or set a bypass token" % _FLOW["captcha"])
    _ids = _FLOW.get("order_ids") or []
    if _ids:
        print("[LT Metrics] Order ids: %s" % ", ".join(_ids[:30]))
    _tl = _FLOW.get("timeline") or []
    if _tl:
        print("\n[LT Metrics] Checkout timeline (last checkout):")
        for _e in _tl:
            _mark = "OK  " if _e.get("ok") else "FAIL"
            _extra = (" -> " + str(_e["extra"])) if _e.get("extra") else ""
            print("  [%s] %-26s %s  %sms%s"
                  % (_mark, _e.get("step"), _e.get("status"), _e.get("ms"), _extra))
    _cs = _FLOW.get("checkout_state") or {}
    if _cs:
        print("\n[LT Metrics] Checkout State Report  (mode=%s, build=%s)"
              % (_FLOW.get("mode"), _FLOW.get("build")))
        if _FAITHFUL_FORCED_OFF:
            print("  note          : faithful/JMeter mode was AUTO-SWITCHED to rest-checkout "
                  "(recording has REST carts/mine write calls that cannot place an order when "
                  "replayed verbatim — empty token-cart + single-use form_key/gateway params)")
        print("  reached state : %s" % _cs.get("state"))
        print("  cart id       : %s" % _cs.get("cart_id"))
        print("  items in cart : %s" % _cs.get("item_count"))
        print("  shipping meths: %s" % _cs.get("shipping_methods"))
        print("  payment meths : %s" % _cs.get("payment_methods"))
        _qt = _cs.get("quote_trace") or []
        if _qt:
            print("  quote trace   :")
            for _q in _qt:
                _n = ("  (%s)" % _q["note"]) if _q.get("note") else ""
                print("      %-34s quote=%s%s" % (_q.get("stage"), _q.get("quote_id") or "-", _n))
            if _cs.get("quote_mismatch"):
                print("      >> QUOTE MISMATCH — add-to-cart and the token's active quote differ")
        if _cs.get("stopped_at"):
            print("  >> STOPPED at '%s' (%s): %s"
                  % (_cs.get("stopped_at"), _cs.get("stop_status"), _cs.get("stop_reason")))
        if _cs.get("order_id"):
            print("  >> ORDER ID   : %s" % _cs.get("order_id"))
    _write_stats()
'''
    # Business groups (Taurus transactions) + their Percent-Executions throughput.
    # Default every group to 100% so a run with no per-group setting behaves
    # exactly as before. The UI/run can lower a group's % to run it in only that
    # fraction of iterations (JMeter Throughput Controller "Percent Executions").
    _groups_seen = []
    for _s in flow_steps:
        _g = (_s.get("group") or "").strip()
        if _g and _g not in _groups_seen:
            _groups_seen.append(_g)
    _gt = plan_cfg.get("group_throughput") or {}
    group_pct = {}
    for _g in _groups_seen:
        try:
            group_pct[_g] = float(_gt.get(_g, 100))
        except (TypeError, ValueError):
            group_pct[_g] = 100.0
    # Our own calls first, then the recording's groups on top. A group the
    # tester actually named wins; ours only fills what the recording never
    # wrapped. Empty strings are dropped so a groupless recorded step falls
    # through to the canonical label instead of blanking it.
    name_group = dict(_kb_stage_map(str(discovery.get("platform") or "")))
    name_group.update({s["name"]: s["group"] for s in flow_steps
                       if (s.get("group") or "").strip()})

    # Auto-switch guardrail: faithful/verbatim replay CANNOT place an order on a
    # Magento REST checkout — the customer token's carts/mine quote is empty when
    # the cart was built via session endpoints, and single-use form_keys / hosted-
    # gateway params can't be replayed. So when the recording contains REST
    # carts/mine WRITE calls, force rest-checkout mode regardless of the requested
    # faithful flag (pure-HTML JMeter recordings keep faithful — no REST writes).
    _rest_write = any(
        s.get("rest") and any(
            w in str(s.get("path") or "").lower()
            for w in ("shipping-information", "set-payment-information",
                      "payment-information"))
        for s in flow_steps)
    faithful_effective = bool(faithful) and not _rest_write
    faithful_forced_off = bool(faithful) and _rest_write

    # Crawl->assertion loop: merge any browser-LEARNED order-success signal (Memory,
    # via _applied_memory) into the KB defaults, so what the browser observed once
    # becomes a standing order-completion assertion on every later run. Deduped.
    _order_url_signals = list(_kb_list("order_url_signals", (
        "onepage/success", "checkout/success", "checkout/onepage/success",
        "order-received", "thank_you", "thankyou", "checkout/thank",
        "order-confirmation", "/thank-you", "quote/success", "quote-submitted",
        "negotiable_quote/quote/view", "rfq/success")))
    _learned_order_sig = str((discovery.get("_applied_memory") or {}).get(
        "order_url_signal") or "").strip()
    if _learned_order_sig and _learned_order_sig not in _order_url_signals:
        _order_url_signals.append(_learned_order_sig)

    # Data->thread sharing mode (JMeter-style). Default "all_threads" == current
    # behavior; "unique" gives each concurrent user a distinct account/row.
    _data_sharing = str((plan_cfg or {}).get("data_sharing") or "all_threads").lower()
    if _data_sharing not in ("all_threads", "unique"):
        _data_sharing = "all_threads"

    repl = {
        "__BASE__": str(discovery.get("base_url")),
        "__DOMAIN__": str(discovery.get("domain")),
        "__GROUP_PCT__": repr(group_pct),
        "__NAME_GROUP__": repr(name_group),
        "__LABEL__": str(plan_cfg["label"]),
        "__USERS__": str(plan_cfg["users"]),
        "__DUR__": str(plan_cfg["duration_human"]),
        "__NSTEPS__": str(len(flow_steps)),
        "__TMIN__": str(tmin),
        "__TMAX__": str(tmax),
        "__WAIT_TIME__": _wait_time_expr(plan_cfg, tmin, tmax),
        "__HAS_REST__": "True" if any(s.get("rest") for s in flow_steps) else "False",
        "__HAS_LOGIN_STEP__": "True" if any(s.get("login") for s in flow_steps) else "False",
        "__LOGIN_URLS__": repr(login_urls),
        "__REST_PREFIX__": repr(rest_prefix),
        "__CART_QTY__": str(max(1, int(cart_qty or 1))),
        "__REST_SKUS__": repr(rest_skus),
        "__NEEDS_REST_CART__": "True" if needs_rest_cart else "False",
        "__FAITHFUL__": "True" if faithful_effective else "False",
        "__FAITHFUL_FORCED_OFF__": "True" if faithful_forced_off else "False",
        "__STRICT__": "True" if bool((plan_cfg or {}).get("strict")) else "False",
        "__DATA_SHARING__": repr(_data_sharing),
        "__PAY_API__": repr(_build_pay_api(discovery, plan_cfg)),
        # Order/quote success signals — KB-overridable (platform_rules), defaults =
        # the standard cross-platform set. A crawl-observed success URL/pattern can
        # be added to the KB to assert order completion per store without code edits.
        "__ORDER_URL_SIGNALS__": repr(tuple(_order_url_signals)),
        "__ORDER_PLACE_PATTERNS__": repr(_kb_list("order_place_patterns", (
            "payment-information", "placeorder", "place-order", "saveorder",
            "checkout/onepage/save", "purchaseorder/save", "wc-ajax=checkout",
            "/checkout.json", "submitorder", "complete-order", "createorder",
            "negotiable-quote", "negotiablequote", "negotiable_quote",
            "requestforquote", "request-for-quote", "request-quote",
            "quote/save", "quotes/mine", "submitquote", "/rfq"))),
        "__BUILD__": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        # learned facts this target taught earlier runs (drop verbose KB rule.* keys)
        "__APPLIED_MEMORY__": repr({k: v for k, v in
                                    (discovery.get("_applied_memory") or {}).items()
                                    if not str(k).startswith("rule.")}),
        # Business Flow Model states pulled from the Knowledge Base (patterns.yaml)
        "__FLOW_MODEL__": repr(_business_flow_states()),
        # Payment policy + endpoints + abort threshold — all from the KB / config.
        "__OFFLINE_PAYMENTS__": repr(_kb_list("offline_payments",
            ("purchaseorder", "checkmo", "netterms", "paymentonaccount",
             "payment_on_account", "companycredit", "banktransfer", "wirepayment",
             "wiretransfer", "wire", "cashondelivery", "cashon", "moneyorder",
             "payorder", "free", "zeropayment", "nopayment", "offlinepayment",
             "offline", "check"))),
        "__HOSTED_GATEWAYS__": repr(_kb_list("hosted_gateways",
            ("cybersource", "paradoxlabs", "adyen", "stripe", "braintree",
             "authorizenet", "authorize_net", "payflow", "worldpay", "sagepay",
             "klarna", "paypal", "amazon", "checkout_com", "checkoutcom",
             "mollie", "square"))),
        "__ENDPOINTS__": repr(_kb_endpoints()),
        "__ABORT_AFTER__": str(int(abort_after)),
        "__AGREEMENT_IDS__": repr([str(a) for a in (agreement_ids or [])]),
        "__CAPTCHA_TOKEN__": repr(str(captcha_token or "")),
        "__CAPTCHA_FIELD__": repr(str(captcha_field or "")),
        "__EXTRA_HEADERS__": repr(extra_headers),
        "__MAX_ORDERS_PER_USER__": repr(int((plan_cfg or {}).get("orders_per_user") or 0)),
        "__FORCED_PAYMENT__": repr(forced_payment),
        "__PAYMENT_METHOD_DETECTION_REASON__": (
            f"# Payment method will be selected dynamically at runtime based on:\n"
            f"# 1) What the target site actually offers (queried from REST API)\n"
            f"# 2) Preference for offline methods (netterms, purchaseorder, etc.) for load testing\n"
            f"# 3) Fallback to non-hosted methods if offline unavailable\n"
            f"# 4) Use hosted gateway if that's the only option\n"
            if not forced_payment
            else (
                f"# Forced payment method from user config or repair hints: {forced_payment}"
            )
        ),
        "__PAYMENT_ADDL__": repr(payment_addl),
        "__PARAM_MAP__": repr(param_map),
        "__CORRELATIONS__": repr(corr_rules),
        "__CHECKOUT_W__": str(max(1, int(discovery.get("checkout_pct", 30)))),
        "__BROWSE_TASKS__": _browse_task_methods(
            discovery, max(0, 100 - int(discovery.get("checkout_pct", 30)))),
        "__FLOW__": repr(flow_steps),
    }
    out = tmpl
    for key, val in repl.items():
        out = out.replace(key, val)
    return out


def _journey_md(discovery: dict) -> str:
    out = [f"# User Journey Map — {discovery.get('domain')}",
           f"\nTarget: {discovery.get('base_url')}",
           f"\nDetected tech: {', '.join(discovery.get('tech') or ['unknown'])}\n"]
    for j in discovery.get("journeys", []):
        out.append(f"## {j['name']} ({j['weight']}%)")
        out.append(" → ".join(j["steps"]) + "\n")
    if discovery.get("apis"):
        out.append("## Inferred API / XHR endpoints")
        for a in discovery["apis"][:20]:
            out.append(f"- `{a['method']} {a['endpoint']}`")
    return "\n".join(out) + "\n"
