"""Discovery & Context Agent.

Given any base URL, crawl the target application (HTTP + HTML parsing), detect
its technology stack and business domain, and map a realistic user journey with
zero technical input. Falls back gracefully on JS-heavy or protected sites so
the pipeline can always continue.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..config import USER_AGENT

# ---- Signature tables ------------------------------------------------------

TECH_SIGNATURES = {
    "Next.js": ["__next", "/_next/static", "next/dist"],
    "React": ["react", "data-reactroot", "__reactcontainer"],
    "Angular": ["ng-version", "ng-app", "angular"],
    "Vue.js": ["vue", "data-v-", "__vue__"],
    "Magento": ["magento", "mage/", "/static/version", "form_key", "checkout/cart"],
    "Shopify": ["cdn.shopify", "shopify", "myshopify.com"],
    "WooCommerce": ["woocommerce", "wp-content/plugins/woocommerce"],
    "WordPress": ["wp-content", "wp-includes", "wp-json"],
    "Salesforce Commerce": ["demandware", "dwstore", "sfcc", "/on/demandware.store"],
    "Drupal": ["drupal", "sites/all", "/sites/default/files"],
    "Django": ["csrfmiddlewaretoken", "__admin_media_prefix__"],
}

DOMAIN_KEYWORDS = {
    "E-commerce": ["cart", "checkout", "add-to-cart", "product", "shop", "basket",
                   "sku", "price", "buy now", "add to bag", "wishlist"],
    "Marketplace": ["seller", "vendor", "marketplace", "storefront", "listing", "offers"],
    "SaaS": ["login", "sign in", "dashboard", "pricing", "free trial", "subscribe",
             "api", "workspace", "account settings"],
    "Content / Media": ["article", "blog", "read more", "subscribe", "newsletter",
                        "category", "author", "comments"],
    "Banking / Finance": ["account balance", "transfer", "transaction", "loan",
                          "invest", "portfolio", "statement"],
    "Travel / Booking": ["book now", "flight", "hotel", "reservation", "check-in",
                         "destination", "one way", "round trip"],
}

SEARCH_HINTS = ["q", "query", "search", "keyword", "s", "term"]
LOGIN_HINTS = ["login", "signin", "sign-in", "log-in", "auth", "session", "account/login"]


def _norm_base(url: str) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _same_host(base: str, link: str) -> bool:
    return urlparse(base).netloc == urlparse(link).netloc


def _fetch(session: requests.Session, url: str, timeout: int = 15):
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        return r
    except requests.RequestException:
        return None


def _detect_tech(html: str, headers: dict) -> list[str]:
    found = []
    low = html.lower()
    server = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
    for tech, sigs in TECH_SIGNATURES.items():
        if any(s in low or s in server for s in sigs):
            found.append(tech)
    # header-only hints
    if "x-powered-by" in {k.lower() for k in headers}:
        found.append(headers.get("X-Powered-By", headers.get("x-powered-by", "")))
    return sorted(set(f for f in found if f))


def _detect_domain(html: str, path_corpus: str) -> tuple[str, dict]:
    low = (html + " " + path_corpus).lower()
    scores = {}
    for domain, kws in DOMAIN_KEYWORDS.items():
        scores[domain] = sum(low.count(k) for k in kws)
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        best = "Generic Web Application"
    return best, scores


def _cdn_waf(headers: dict) -> list[str]:
    hits = []
    h = {k.lower(): v for k, v in headers.items()}
    if "cf-ray" in h or "cloudflare" in str(h.get("server", "")).lower():
        hits.append("Cloudflare")
    if "x-amz-cf-id" in h or "cloudfront" in str(h.get("via", "")).lower():
        hits.append("AWS CloudFront")
    if "x-akamai-transformed" in h or "akamai" in str(h.get("server", "")).lower():
        hits.append("Akamai")
    if "x-cache" in h:
        hits.append("Edge cache")
    return hits


def crawl(url: str, max_pages: int = 12, timeout: int = 15,
          credentials: dict | None = None, render_js: bool | None = None) -> dict:
    """Crawl `url` breadth-first and return a structured discovery report.

    `render_js` controls the optional headless-browser (Playwright) re-crawl
    for JavaScript-rendered sites:
      * None (default) — auto: only kicks in if the plain HTTP+HTML crawl
        below detects a JS framework (React/Angular/Vue/Next.js) AND comes
        back sparse (no forms, effectively one page) — i.e. only in the
        exact situation this function's own docstring already warned about.
        A no-op, byte-for-byte identical to the old behavior, on every site
        that doesn't hit that condition, and a no-op everywhere if Playwright
        isn't installed (it's an optional extra — see requirements.txt).
      * True — always attempt the Playwright re-crawl.
      * False — never attempt it, even if it looks like it would help.
    """
    base = _norm_base(url)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "text/html,application/xhtml+xml"})

    result = {
        "base_url": base,
        "start_url": url if re.match(r"^https?://", url) else base,
        "reachable": False,
        "status_code": None,
        "final_url": None,
        "title": None,
        "tech": [],
        "cdn_waf": [],
        "domain": "Unknown",
        "domain_scores": {},
        "pages": [],       # discovered GET pages -> {name, path, method:GET}
        "forms": [],       # discovered forms    -> {name, action, method, inputs, kind}
        "apis": [],        # inferred API/XHR endpoints
        "journeys": [],
        "search_endpoint": None,
        "login_form": None,
        "notes": [],
    }

    start = result["start_url"]
    root = _fetch(session, start, timeout)
    if root is None:
        result["notes"].append("Target did not respond; using generic assumptions.")
        result["domain"] = "Generic Web Application"
        result["journeys"] = _fallback_journeys(base)
        result["pages"] = [{"name": "Home Page", "path": "/", "method": "GET"}]
        return result

    result["reachable"] = True
    result["status_code"] = root.status_code
    result["final_url"] = root.url
    home_html = root.text or ""
    soup = BeautifulSoup(home_html, "html.parser")
    result["title"] = (soup.title.string.strip() if soup.title and soup.title.string else None)
    result["tech"] = _detect_tech(home_html, dict(root.headers))
    result["cdn_waf"] = _cdn_waf(dict(root.headers))

    # BFS crawl for internal links
    visited: set[str] = set()
    queue: deque[str] = deque([root.url])
    page_records: dict[str, dict] = {}
    forms_seen: list[dict] = []
    api_hints: set[str] = set()
    corpus_paths: list[str] = []

    while queue and len(visited) < max_pages:
        cur = queue.popleft()
        if cur in visited:
            continue
        visited.add(cur)
        resp = root if cur == root.url else _fetch(session, cur, timeout)
        if resp is None or "text/html" not in resp.headers.get("Content-Type", ""):
            continue
        html = resp.text or ""
        s = BeautifulSoup(html, "html.parser")
        path = urlparse(resp.url).path or "/"
        corpus_paths.append(path)
        name = _label_for_path(path, s)
        page_records.setdefault(path, {"name": name, "path": path, "method": "GET"})

        # collect forms
        for f in s.find_all("form"):
            rec = _parse_form(f, resp.url)
            if rec and rec["action"] not in {x["action"] for x in forms_seen}:
                forms_seen.append(rec)

        # find inline API/XHR references
        for m in re.finditer(r"""["'](/[^"'<> ]*(?:api|graphql|rest|/ajax/|\.json)[^"'<> ]*)["']""",
                             html, re.IGNORECASE):
            api_hints.add(m.group(1))

        # enqueue same-host links
        for a in s.find_all("a", href=True):
            link = urljoin(resp.url, a["href"].split("#")[0])
            if link.startswith("http") and _same_host(base, link) and link not in visited:
                if len(visited) + len(queue) < max_pages * 3:
                    queue.append(link)

    result["pages"] = list(page_records.values())[:max_pages]
    result["forms"] = forms_seen
    result["apis"] = [{"method": "GET", "endpoint": p} for p in sorted(api_hints)][:25]

    # domain detection
    domain, scores = _detect_domain(home_html, " ".join(corpus_paths))
    result["domain"] = domain
    result["domain_scores"] = scores

    # classify search + login forms
    for f in forms_seen:
        inputs = [i["name"].lower() for i in f["inputs"] if i.get("name")]
        action_low = f["action"].lower()
        if any(h in inputs for h in SEARCH_HINTS) or "search" in action_low:
            f["kind"] = "search"
            if not result["search_endpoint"]:
                sp = [i["name"] for i in f["inputs"]
                      if i.get("name", "").lower() in SEARCH_HINTS]
                result["search_endpoint"] = {
                    "action": f["action"], "method": f["method"],
                    "param": sp[0] if sp else "q",
                }
        if any(h in action_low for h in LOGIN_HINTS) or (
                any("pass" in i for i in inputs) and any(
                    x in i for i in inputs for x in ("user", "email", "login"))):
            f["kind"] = "login"
            result["login_form"] = f

    result["journeys"] = _build_journeys(result, credentials)
    if not result["pages"]:
        result["pages"] = [{"name": "Home Page", "path": "/", "method": "GET"}]

    # ---- optional JS-rendered re-crawl (Playwright) ------------------------
    if render_js is True or (render_js is None and _looks_js_heavy_and_sparse(result)):
        enhanced = _playwright_crawl(url, max_pages, timeout, credentials, base)
        if enhanced is not None:
            enhanced.setdefault("notes", []).append(
                "Static HTTP crawl found little content on this JS-rendered "
                "site — re-crawled with a headless browser (Playwright) to "
                "see the real rendered DOM and the API calls it makes.")
            return enhanced
        elif render_js is True:
            result["notes"].append(
                "render_js was requested but Playwright isn't available — "
                "install it (`pip install playwright && playwright install "
                "chromium`) to enable the headless-browser re-crawl.")
        elif result["tech"] and _looks_js_heavy_and_sparse(result):
            result["notes"].append(
                "This looks like a JS-rendered app (" + ", ".join(result["tech"]) +
                ") and the static crawl found little content. Install Playwright "
                "(`pip install playwright && playwright install chromium`) and "
                "re-run, or pass render_js=True, for a fuller crawl.")
    return result


def _looks_js_heavy_and_sparse(result: dict) -> bool:
    """True when the plain HTTP+BeautifulSoup crawl above almost certainly
    missed real content because the page needs JS to render — i.e. exactly
    the case this module's docstring already calls out as a known gap."""
    js_frameworks = {"React", "Angular", "Next.js", "Vue.js"}
    if not (set(result.get("tech") or []) & js_frameworks):
        return False
    return len(result.get("pages") or []) <= 1 and not result.get("forms")


def crawl_and_record(url: str, max_pages: int = 12, timeout: int = 20,
                     credentials: dict | None = None, search_term: str = "test") -> dict | None:
    """Explicit entry point for "crawl this site AND drive it through a real
    journey" — the standalone alternative to uploading a recording. Unlike
    `crawl()`'s auto-heuristic Playwright path (passive, only for sparse
    JS-heavy sites), this always uses Playwright and always attempts guided
    interactions: submit a search, log in if credentials are given, add a
    product to cart, view the cart. Returns None if Playwright isn't
    installed or the browser crawl fails outright — never raises.
    """
    return _playwright_crawl(url, max_pages, timeout, credentials, _norm_base(url),
                             active=True, search_term=search_term)


_PLAYWRIGHT_WALL_CLOCK_BUDGET = 60  # seconds — hard cap for the whole crawl+journey
                                     # pass, so a site that never goes network-idle
                                     # fails fast with a clear note instead of
                                     # silently grinding for minutes.


def _playwright_crawl(url: str, max_pages: int, timeout: int,
                      credentials: dict | None, base: str,
                      active: bool = False, search_term: str = "test") -> dict | None:
    """Re-crawl `url` with a real headless browser so client-side-rendered
    content (and the API calls it triggers) are actually visible. Returns
    None — never raises — if Playwright isn't installed or anything about
    the browser crawl fails, so the caller always has the static result to
    fall back on. This is intentionally the SAME shape as `crawl()`'s return
    value (reuses the same HTML-parsing helpers) so nothing downstream needs
    to know which crawler produced it.

    `active=True` (used by `crawl_and_record()`) additionally drives a
    best-effort journey — search, login, add-to-cart, cart view — on top of
    the passive link-following crawl below; `active=False` (used by the
    auto-heuristic path in `crawl()`) never does, to keep that path exactly
    as it was.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    result = {
        "base_url": base, "start_url": url, "reachable": False,
        "status_code": None, "final_url": None, "title": None,
        "tech": [], "cdn_waf": [], "domain": "Unknown", "domain_scores": {},
        "pages": [], "forms": [], "apis": [], "flow": [], "journeys": [],
        "search_endpoint": None, "login_form": None, "notes": [],
        "rendered_with": "playwright",
    }
    visited: set[str] = set()
    queue: deque[str] = deque([url])
    page_records: dict[str, dict] = {}
    forms_seen: list[dict] = []
    api_hints: set[str] = set()
    flow_steps: list[dict] = []
    corpus_paths: list[str] = []
    last_html = ""
    deadline = time.monotonic() + _PLAYWRIGHT_WALL_CLOCK_BUDGET

    def _on_request(request):
        # Real XHR/fetch/document POSTs the rendered page actually makes —
        # far more reliable than regex-scraping inline JS for API-looking
        # strings, and this feeds directly into the same recording-derived
        # parameterization used for uploaded recordings (generator.py).
        if request.resource_type not in ("xhr", "fetch", "document"):
            return
        try:
            u = urlparse(request.url)
            if not _same_host(base, request.url):
                return
            body = None
            if request.method in ("POST", "PUT", "PATCH"):
                data = request.post_data
                if data:
                    try:
                        body = json.loads(data)
                    except Exception:
                        body = data
            flow_steps.append({"method": request.method, "path": u.path, "body": body})
            if any(k in u.path.lower() for k in ("api", "graphql", "rest", "/ajax/", ".json")):
                api_hints.add(u.path)
        except Exception:
            pass

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=USER_AGENT)
                page = context.new_page()
                page.on("request", _on_request)
                page.set_default_timeout(timeout * 1000)

                while queue and len(visited) < max_pages:
                    if time.monotonic() > deadline:
                        result["notes"].append(
                            f"Playwright: stopped the crawl early after "
                            f"{_PLAYWRIGHT_WALL_CLOCK_BUDGET}s to avoid a long hang "
                            f"({len(visited)} of up to {max_pages} pages visited).")
                        break
                    cur = queue.popleft()
                    if cur in visited:
                        continue
                    visited.add(cur)
                    try:
                        resp = page.goto(cur, wait_until="domcontentloaded")
                        # Brief grace period, not a full networkidle wait — some
                        # sites (chat widgets, analytics, ads) never go fully
                        # idle, which used to make every page eat its whole
                        # timeout. This still lets async XHR/fetch calls fire
                        # (captured by _on_request) without the long hang.
                        page.wait_for_timeout(1000)
                    except Exception:
                        continue
                    if resp is None:
                        continue
                    if cur == url:
                        result["reachable"] = True
                        result["status_code"] = resp.status
                        result["final_url"] = page.url
                    html = page.content()
                    last_html = html
                    soup = BeautifulSoup(html, "html.parser")
                    if result["title"] is None and soup.title and soup.title.string:
                        result["title"] = soup.title.string.strip()
                    if not result["tech"]:
                        result["tech"] = _detect_tech(html, dict(resp.headers))
                        result["cdn_waf"] = _cdn_waf(dict(resp.headers))

                    path = urlparse(page.url).path or "/"
                    corpus_paths.append(path)
                    page_records.setdefault(path, {
                        "name": _label_for_path(path, soup), "path": path, "method": "GET"})

                    for f in soup.find_all("form"):
                        rec = _parse_form(f, page.url)
                        if rec and rec["action"] not in {x["action"] for x in forms_seen}:
                            forms_seen.append(rec)

                    for a in soup.find_all("a", href=True):
                        link = urljoin(page.url, a["href"].split("#")[0])
                        if (link.startswith("http") and _same_host(base, link)
                                and link not in visited
                                and len(visited) + len(queue) < max_pages * 3):
                            queue.append(link)

                if active:
                    if time.monotonic() > deadline:
                        result["notes"].append(
                            "Playwright: skipped the guided journey (search/login/"
                            "add-to-cart/cart) — the wall-clock budget was already "
                            "used up by the passive crawl.")
                    else:
                        try:
                            _attempt_active_journey(page, base, result, credentials,
                                                    search_term, page_records, forms_seen,
                                                    corpus_paths, deadline)
                        except Exception as exc:
                            result["notes"].append(f"Playwright: guided journey failed ({exc}).")
            finally:
                browser.close()
    except Exception:
        return None

    if not result["reachable"]:
        return None

    # a small buffer beyond max_pages so the (up to 4) guided-journey pages
    # from _attempt_active_journey — Search Results, Login, PDP, Cart — never
    # get silently truncated just because the passive BFS above already used
    # up the page budget.
    result["pages"] = list(page_records.values())[:max_pages + 6]
    result["forms"] = forms_seen
    result["apis"] = [{"method": "GET", "endpoint": p} for p in sorted(api_hints)][:25]
    result["flow"] = flow_steps[:200]
    domain, scores = _detect_domain(last_html, " ".join(corpus_paths))
    result["domain"] = domain
    result["domain_scores"] = scores

    for f in forms_seen:
        inputs = [i["name"].lower() for i in f["inputs"] if i.get("name")]
        action_low = f["action"].lower()
        if any(h in inputs for h in SEARCH_HINTS) or "search" in action_low:
            f["kind"] = "search"
            if not result["search_endpoint"]:
                sp = [i["name"] for i in f["inputs"] if i.get("name", "").lower() in SEARCH_HINTS]
                result["search_endpoint"] = {
                    "action": f["action"], "method": f["method"],
                    "param": sp[0] if sp else "q"}
        if any(h in action_low for h in LOGIN_HINTS) or (
                any("pass" in i for i in inputs) and any(
                    x in i for i in inputs for x in ("user", "email", "login"))):
            f["kind"] = "login"
            result["login_form"] = f

    result["journeys"] = _build_journeys(result, credentials)
    if not result["pages"]:
        result["pages"] = [{"name": "Home Page", "path": "/", "method": "GET"}]
    return result


def _attempt_active_journey(page, base: str, result: dict, credentials: dict | None,
                            search_term: str, page_records: dict, forms_seen: list,
                            corpus_paths: list, deadline: float | None = None) -> None:
    """Best-effort guided journey on top of the passive crawl in
    `_playwright_crawl`: submit a search, log in if credentials were given,
    add a product to cart, then view the cart. Every step is independent and
    wrapped so one failing doesn't stop the others or the crawl overall —
    each outcome (success or not) is recorded in `result["notes"]` so it's
    obvious what was and wasn't captured automatically.

    Deliberately stops at add-to-cart / cart view. Checkout and payment forms
    vary far too much (address fields, shipping options, payment providers,
    CAPTCHA) to drive blindly — that part still benefits from an uploaded
    recording, and the notes say so explicitly rather than pretending this
    covers the whole journey.
    """

    def _rescan(label: str):
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        path = urlparse(page.url).path or "/"
        corpus_paths.append(path)
        page_records.setdefault(path, {"name": label or _label_for_path(path, soup),
                                       "path": path, "method": "GET"})
        for f in soup.find_all("form"):
            rec = _parse_form(f, page.url)
            if rec and rec["action"] not in {x["action"] for x in forms_seen}:
                forms_seen.append(rec)

    def _time_up() -> bool:
        return deadline is not None and time.monotonic() > deadline

    # 1) Search
    try:
        param = (result.get("search_endpoint") or {}).get("param", "q")
        box = page.locator(
            f'input[name="{param}"], input[type="search"], '
            'input[name*="search" i], input[name*="query" i], input[name*="keyword" i]'
        ).first
        if box.count():
            box.fill(search_term)
            box.press("Enter")
            page.wait_for_load_state("networkidle", timeout=8000)
            _rescan("Search Results")
            result["notes"].append(f'Playwright: submitted a search for "{search_term}".')
        else:
            result["notes"].append("Playwright: no search box found to submit automatically.")
    except Exception as exc:
        result["notes"].append(f"Playwright: search attempt failed ({exc}).")

    # 2) Login
    if _time_up():
        if credentials and credentials.get("username"):
            result["notes"].append(
                "Playwright: skipped login — wall-clock budget already used up.")
    elif credentials and credentials.get("username"):
        try:
            login_form = result.get("login_form") or {}
            target = login_form.get("full_action") or (
                urljoin(base, login_form["action"]) if login_form.get("action") else None)
            if target:
                page.goto(target, wait_until="domcontentloaded")
                page.wait_for_timeout(1000)
            user_box = page.locator(
                'input[type="email"], input[name*="user" i], input[name*="login" i], '
                'input[name*="email" i]').first
            pass_box = page.locator('input[type="password"]').first
            if user_box.count() and pass_box.count():
                user_box.fill(credentials["username"])
                pass_box.fill(credentials.get("password") or "")
                pass_box.press("Enter")
                page.wait_for_load_state("networkidle", timeout=8000)
                _rescan("Login Page")
                result["notes"].append("Playwright: submitted the login form.")
            else:
                result["notes"].append(
                    "Playwright: no login fields found to submit automatically.")
        except Exception as exc:
            result["notes"].append(f"Playwright: login attempt failed ({exc}).")

    # 3) Add a discovered product to the cart
    if _time_up():
        result["notes"].append(
            "Playwright: skipped add-to-cart — wall-clock budget already used up.")
    else:
        try:
            pdp_path = next((p for p, rec in page_records.items()
                             if "Product" in (rec.get("name") or "")), None)
            if pdp_path:
                page.goto(urljoin(base, pdp_path), wait_until="domcontentloaded")
                page.wait_for_timeout(1000)
                btn = page.get_by_role(
                    "button", name=re.compile(r"add.*(cart|bag|basket)", re.I)).first
                if not btn.count():
                    btn = page.locator("button, a").filter(
                        has_text=re.compile(r"add.*(cart|bag|basket)", re.I)).first
                if btn.count():
                    btn.click()
                    page.wait_for_load_state("networkidle", timeout=8000)
                    _rescan("Product Detail (PDP)")
                    result["notes"].append("Playwright: clicked an add-to-cart control.")
                else:
                    result["notes"].append(
                        "Playwright: found a product page but no add-to-cart control to "
                        "click automatically.")
            else:
                result["notes"].append(
                    "Playwright: no product page discovered to add to cart.")
        except Exception as exc:
            result["notes"].append(f"Playwright: add-to-cart attempt failed ({exc}).")

    # 4) View the cart
    if _time_up():
        result["notes"].append(
            "Playwright: skipped cart view — wall-clock budget already used up.")
    else:
        try:
            cart_link = page.locator('a[href*="cart" i], a[href*="basket" i]').first
            if cart_link.count():
                cart_link.click()
                page.wait_for_load_state("networkidle", timeout=8000)
                _rescan("Cart Page")
                result["notes"].append("Playwright: viewed the cart page.")
        except Exception as exc:
            result["notes"].append(f"Playwright: cart view attempt failed ({exc}).")

    result["notes"].append(
        "Playwright's guided crawl stops at add-to-cart — checkout and payment forms vary "
        "too much to drive blindly. Upload a recording of that part if you need it covered.")


def _parse_form(form, page_url: str) -> dict | None:
    action = form.get("action") or page_url
    action = urljoin(page_url, action)
    method = (form.get("method") or "GET").upper()
    inputs = []
    for tag in form.find_all(["input", "select", "textarea"]):
        nm = tag.get("name")
        if not nm:
            continue
        inputs.append({
            "name": nm,
            "type": tag.get("type", tag.name),
            "value": tag.get("value", ""),
        })
    if not inputs:
        return None
    return {"action": urlparse(action).path or "/", "full_action": action,
            "method": method, "inputs": inputs, "kind": "generic"}


def _label_for_path(path: str, soup: BeautifulSoup) -> str:
    p = path.lower().rstrip("/")
    if p in ("", "/"):
        return "Home Page"
    for kw, label in (("cart", "Cart Page"), ("basket", "Cart Page"),
                      ("checkout", "Checkout Page"), ("login", "Login Page"),
                      ("account", "Account Page"), ("search", "Search Results"),
                      ("product", "Product Detail (PDP)"), ("category", "Category (PLP)"),
                      ("pricing", "Pricing Page"), ("blog", "Blog / Article"),
                      ("dashboard", "Dashboard")):
        if kw in p:
            return label
    # derive from last path segment
    seg = p.strip("/").split("/")[-1].replace("-", " ").replace("_", " ").title()
    return seg[:40] or "Page"


def _build_journeys(result: dict, credentials: dict | None) -> list[dict]:
    domain = result["domain"]
    pages = result["pages"]
    has_login = result["login_form"] is not None and bool(credentials)
    labels = [p["name"] for p in pages]

    def has(*names):
        return [n for n in names if n in labels]

    journeys: list[dict] = []
    if domain == "E-commerce" or domain == "Marketplace":
        anon = ["Home Page"] + has("Search Results") + has("Category (PLP)") + \
               has("Product Detail (PDP)")
        journeys.append({"name": "Anonymous Browse", "weight": 60,
                         "steps": anon or ["Home Page"]})
        if result["search_endpoint"]:
            journeys.append({"name": "Search Flow", "weight": 25,
                             "steps": ["Home Page", "Search Results",
                                       "Product Detail (PDP)"]})
        if has_login:
            journeys.append({"name": "Authenticated Cart", "weight": 15,
                             "steps": ["Login Page", "Product Detail (PDP)",
                                       "Cart Page"]})
    elif domain == "SaaS":
        journeys.append({"name": "Marketing Browse", "weight": 55,
                         "steps": ["Home Page"] + has("Pricing Page")})
        if has_login:
            journeys.append({"name": "App Session", "weight": 45,
                             "steps": ["Login Page", "Dashboard"]})
    else:
        journeys.append({"name": "Site Browse", "weight": 70,
                         "steps": [p["name"] for p in pages[:5]] or ["Home Page"]})
        if result["search_endpoint"]:
            journeys.append({"name": "Search Flow", "weight": 30,
                             "steps": ["Home Page", "Search Results"]})

    if not journeys:
        journeys = _fallback_journeys(result["base_url"])
    # normalize weights to 100
    total = sum(j["weight"] for j in journeys) or 1
    for j in journeys:
        j["weight"] = round(j["weight"] * 100 / total)
    return journeys


def _fallback_journeys(base: str) -> list[dict]:
    return [{"name": "Site Browse", "weight": 100, "steps": ["Home Page"]}]
