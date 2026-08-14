"""Generate APEA's browser-track (Track B) load runner.

Writes two files into a run's `scripts/` dir:
  * journey.json          — what to drive (nav steps derived from the recording,
                            plus optional login / payment extension points)
  * playwright_runner.py  — a self-contained async Playwright load runner that
                            spawns a pool of browser contexts (one per virtual
                            user), loops the journey for the test duration, and
                            writes JMeter/Locust-shaped stats.

This module is only imported by the Executor's opt-in Track-B hook. Nothing here
runs unless a run explicitly sets `browser_track.enabled` AND Playwright is
installed — so it cannot affect the existing Locust path.

First slice: the runner drives real page navigations (the recorded GET pages) in
a real browser and measures load timings. Driving payment iFrames / Shadow DOM /
3DS is the `_drive_payment` extension point — gateway-agnostic, fed by whichever
gateway entry in knowledge/rules/browser_patterns.yaml the run's payment config
names (Stripe, ParadoxLabs/CyberSource, ...), and later by the Browser Context Graph.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse


def _nav_steps(discovery: dict) -> list:
    """GET navigations to drive in the browser, derived from the recording's
    pages + GET flow steps. Deduped, capped, root-relative paths kept."""
    steps, seen = [], set()

    def _add(label, path):
        p = (path or "").strip()
        if not p or p in seen:
            return
        # only navigable document paths (skip API/asset-ish query calls)
        low = p.lower()
        if any(k in low for k in ("/rest/", "/api/", "/graphql", "/ajax")):
            return
        seen.add(p)
        steps.append({"label": (label or p)[:60], "path": p})

    for pg in (discovery.get("pages") or []):
        if (pg.get("method") or "GET").upper() == "GET":
            _add(pg.get("name"), pg.get("path"))
    for s in (discovery.get("flow") or []):
        if (s.get("method") or "GET").upper() == "GET":
            _add(s.get("label") or s.get("name"), s.get("path"))
        if len(steps) >= 12:
            break
    if not steps:
        _add("Home", "/")
    return steps[:12]


def _load_browser_patterns() -> dict:
    """KB gateway patterns (iframe selectors, test cards, 3DS). Best-effort."""
    try:
        import yaml
        p = Path(__file__).resolve().parent.parent / "knowledge" / "rules" / "browser_patterns.yaml"
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _build_payment(cfg: dict | None) -> dict | None:
    """Resolve a payment config (from the run) against KB gateway patterns, so the
    runner gets concrete iframe selectors + a sandbox test card baked in. Returns
    None when no payment was requested (runner then drives navigations only)."""
    if not cfg:
        return None
    gw = str(cfg.get("gateway") or "stripe").lower()
    pats = _load_browser_patterns().get(gw, {}) or {}
    scenario = cfg.get("scenario") or "success"
    # Pick the sandbox test card for the requested scenario. If THIS gateway has
    # no card for that scenario (e.g. CyberSource has no "threeds" entry), fall
    # back to the gateway's own "success" card — then its first card — and only
    # as a last resort a generic default. NEVER silently use another gateway's
    # card (the old bug: CyberSource+threeds -> Stripe's 4242…).
    _cards = pats.get("test_cards") or {}
    card = dict(_cards.get(scenario) or _cards.get("success")
                or next(iter(_cards.values()), None)
                or {"number": "4111111111111111", "exp": "12 / 34", "cvc": "123", "postal": "12345"})
    card.update(cfg.get("card") or {})   # explicit card overrides win
    return {
        "gateway": gw,
        "card": card,
        "payment_element_fields": pats.get("payment_element_fields") or {},
        "card_element_fields": pats.get("card_element_fields") or {},
        "frame_url_patterns": pats.get("frame_url_patterns") or ["js.stripe.com"],
        "threeds": pats.get("threeds") or {},
        # site-specific — supplied per run (how to reach + submit the payment form):
        "pay_trigger_selector": cfg.get("pay_trigger_selector") or pats.get("pay_trigger_selector"),
        "submit_selector": cfg.get("submit_selector") or pats.get("submit_selector"),
        "success_url_contains": cfg.get("success_url_contains") or pats.get("success_url_contains"),
        "url": cfg.get("url"),   # optional: navigate here before driving payment
    }


def _recorded_creds(discovery: dict) -> dict:
    """Username/password captured in the recording's Selenium `type` actions, so
    the browser track can log in without a separate CSV. Best-effort."""
    out = {}
    for si in (discovery.get("selenium_inputs") or []):
        f = str(si.get("field") or "").lower()
        v = si.get("sample") or si.get("value")
        if v and f == "username":
            out["username"] = v
        elif v and f == "password":
            out["password"] = v
    return out


def generate_browser_runner(run_dir, target_url: str, plan_cfg: dict,
                            discovery: dict | None = None) -> str | None:
    """Write journey.json + playwright_runner.py into run_dir/scripts.
    Returns the runner path, or None if it could not be prepared.

    The journey is planned intelligently for ANY recording: browser_journey
    detects the gateway and (via Claude when available) infers the checkout
    nav/selectors, so this is not tied to one site. All still opt-in — nothing
    here runs unless the run enabled the browser track."""
    try:
        scripts = Path(run_dir) / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        discovery = discovery or {}

        # 1) plan the journey (deterministic + optional Claude enrichment)
        try:
            from . import browser_journey
            plan = browser_journey.plan_journey(discovery, plan_cfg or {})
        except Exception:
            plan = {}

        # 2) resolve the payment config. The run may name a gateway; otherwise
        #    auto-adopt the DETECTED gateway so APEA drives the card flow it found
        #    in the recording without the user hand-configuring it.
        payment_cfg = ((plan_cfg or {}).get("browser_track") or {}).get("payment")
        if payment_cfg is None and plan.get("gateway"):
            payment_cfg = {"gateway": plan["gateway"], "scenario": "success"}
        if isinstance(payment_cfg, dict):
            payment_cfg = dict(payment_cfg)
            if not payment_cfg.get("gateway") and plan.get("gateway"):
                payment_cfg["gateway"] = plan["gateway"]
            # No gateway chosen AND none detected -> don't drive a bogus default
            # gateway; just navigate (avoids trying Stripe selectors on a non-Stripe
            # site). The browser track still runs, minus the card step.
            if not payment_cfg.get("gateway"):
                payment_cfg = None
            else:
                # fold in plan-inferred site selectors as defaults (run-supplied win)
                for k in ("pay_trigger_selector", "submit_selector", "success_url_contains"):
                    if not payment_cfg.get(k) and plan.get(k):
                        payment_cfg[k] = plan[k]
                if not payment_cfg.get("url") and plan.get("checkout_url"):
                    payment_cfg["url"] = plan["checkout_url"]

        # 3) login: plan selectors + credentials. Run-supplied credentials
        #    (server/CLI, via plan_cfg.browser_track.credentials) take precedence
        #    over any captured in the recording's Selenium `type` actions.
        login = dict(plan.get("login") or {})
        bt_creds = ((plan_cfg or {}).get("browser_track") or {}).get("credentials") or {}
        for src in (_recorded_creds(discovery), bt_creds):
            for k in ("username", "password"):
                if src.get(k):
                    login[k] = src[k]
        # keep login only if there's something to act on (a page to visit,
        # selectors to use, or credentials to type — the runner heuristically
        # finds the login form/path when selectors/url are missing)
        if not (login.get("url") or login.get("username_selector")
                or login.get("username") or login.get("password")):
            login = None

        _pmt = _build_payment(payment_cfg)
        if _pmt is not None and plan.get("recorded_payment"):
            # drive the EXACT payment flow captured in the recording (iframe +
            # field selectors), falling back to KB selectors only if it can't.
            _pmt["recorded_steps"] = plan["recorded_payment"]
        journey = {
            "host": target_url or discovery.get("base_url") or "",
            "steps": plan.get("nav") or _nav_steps(discovery),
            "ui_journey": plan.get("ui_journey") or [],
            # KB resilience fallbacks (per-intent selectors/texts) baked in, so the
            # runner recovers a failed brittle selector without KB access.
            "resilience": _load_browser_patterns().get("resilience") or {},
            "login": login,
            "product_url": plan.get("product_url"),
            "product_search": plan.get("product_search") or {},
            "checkout_url": plan.get("checkout_url"),
            "add_to_cart_selector": plan.get("add_to_cart_selector"),
            "payment_method_selector": plan.get("payment_method_selector"),
            "payment": _pmt,
            "browser_context": discovery.get("browser_context") or {},
        }
        (scripts / "journey.json").write_text(json.dumps(journey, indent=2), encoding="utf-8")
        (scripts / "playwright_runner.py").write_text(_RUNNER_SRC, encoding="utf-8")
        return str(scripts / "playwright_runner.py")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# The runner is a STATIC, self-contained script (reads journey.json + CLI args).
# It is defensive end-to-end: any failure writes a summary note and exits 0 so a
# Track-B problem can never fail the overall run.
# --------------------------------------------------------------------------- #
_RUNNER_SRC = r'''#!/usr/bin/env python3
"""APEA browser-track (Track B) load runner — generated, do not edit by hand.

Spawns a pool of Chromium browser CONTEXTS (one per virtual user), each looping
the journey in journey.json for the test duration, and writes:
  results/playwright_stats.csv     Locust/JMeter-shaped per-transaction stats
  results/browser_track.json       summary (vus, iterations, errors, note)
  results/apea_calls_browser.jsonl per-navigation feed (kept separate from the
                                   HTTP feed so the two processes never corrupt
                                   each other's file)

Payment iFrame / Shadow DOM / 3DS driving is the `_drive_payment` extension
point — gateway-agnostic (driven by journey.payment, resolved from KB gateway
patterns at generation time); a no-op if no payment was configured for this run.
"""
import argparse, asyncio, json, os, random, re, statistics, time

# Generic hosted-gateway iframe hosts — used to LOCATE the card iframe even when
# the KB frame patterns don't match (so any app's gateway can be found).
_KNOWN_GATEWAY_HOSTS = ["js.stripe.com", "stripe", "cybersource", "secureacceptance",
                        "flex.cybersource", "adyen", "checkoutshopper", "braintree",
                        "paypal", "checkout.com"]
# Common Pay / Place-order button texts + selectors, tried when no submit selector
# was supplied or inferred (platform-agnostic heuristics).
_SUBMIT_SELECTORS = ["#place-order", "button.action.primary.checkout",
                     "button.checkout", "button[type=submit]", "input[type=submit]"]
_SUBMIT_TEXTS = ["Place Order", "Place order", "Pay now", "Pay", "Complete order",
                 "Submit order", "Confirm order", "Confirm & pay", "Buy now"]
# Generic login heuristics (used when the planner didn't supply url/selectors).
_LOGIN_PATHS = ["/customer/account/login", "/login", "/account/login",
                "/signin", "/sign-in", "/auth/login"]
_USER_SELECTORS = ["input[type=email]", "input[name=email]", "#email",
                   "input[name=username]", "#username", "input[id*=email]",
                   "input[name='login[username]']", "input[autocomplete=username]"]
_PASS_SELECTORS = ["input[type=password]", "#pass", "input[name=password]",
                   "input[name='login[password]']", "input[autocomplete=current-password]"]
_LOGIN_SUBMIT_TEXTS = ["Sign in", "Log in", "Login", "Sign In", "Log In", "Continue"]
# Price capture — read the REAL rendered price the browser sees (HTTP replay sees
# 0 when price is computed client-side). Currency-amount regex + common price
# containers; platform-agnostic.
_PRICE_RE = re.compile(r"[£$€]\s?\d[\d,]*(?:\.\d{2})?")
_PRICE_SELECTORS = ["[data-price-amount]", ".price-wrapper .price", ".product-info-price .price",
                    "span.price", ".price", "[class*=price]"]
# Add-to-cart + checkout-progression heuristics (Magento-standard + text-based).
_ADDCART_SELECTORS = ["#product-addtocart-button", "button[id*=addtocart]",
                      "button.tocart", "button.add-to-cart", "button[title*='Add to' i]"]
_ADDCART_TEXTS = ["Add to Cart", "Add to Basket", "Add to bag", "Add to cart", "Add to basket"]
_PROCEED_TEXTS = ["Proceed to Checkout", "Go to Checkout", "Checkout", "Continue to checkout"]
_NEXT_TEXTS = ["Review & Payments", "Continue to payment", "Next", "Continue", "Proceed"]
_PAY_RADIO_SELECTORS = ["input#paradoxlabs_cybersource", "input[value*=cybersource]",
                        "input[value*=paradoxlabs]", "input[id*=cybersource]",
                        "input[id*=creditcard]", "input[value*=card]"]

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
_RUN_DIR = os.path.dirname(_SCRIPTS)
_RESULTS = os.path.join(_RUN_DIR, "results")
_JOURNEY = os.path.join(_SCRIPTS, "journey.json")
_STATS   = os.path.join(_RESULTS, "playwright_stats.csv")
_SUMMARY = os.path.join(_RESULTS, "browser_track.json")
_CALLS   = os.path.join(_RESULTS, "apea_calls_browser.jsonl")

_samples = {}   # label -> [ms]
_fails   = {}   # label -> int
_seq     = [0]
_iters   = [0]
_prices  = []   # [{url, price}] — real rendered prices the browser saw
_payreqs = []   # captured payment gateway/iframe network calls (REDACTED) — the
                # evidence a replay-profile classifier uses to decide whether the
                # card step is HTTP-replayable. PANs/secrets are stripped on capture.
_order_confirm = {}  # ground-truth order-success signal the BROWSER observed (URL
                # fragment / phrase / order number). This is the crawl-derived
                # order-confirmation assertion: the HTTP load script can be checked
                # against it, and the signal reused to assert future runs.
# Fail-closed payment outcome for THIS run. required=a card gateway was selected;
# ok=the card actually cleared. If required and not ok, the run must FAIL — the
# card is never quietly skipped and no other method is substituted.
_pay     = {"required": False, "ok": False, "err": "", "gateway": ""}


def _record(label, ms, ok):
    _samples.setdefault(label, []).append(ms)
    if not ok:
        _fails[label] = _fails.get(label, 0) + 1


def _log_call(name, method, url, status, ok, ms, err=""):
    try:
        os.makedirs(_RESULTS, exist_ok=True)
        entry = {"seq": _seq[0], "ts": round(time.time(), 3), "group": "Browser",
                 "name": name, "method": method, "url": url, "status": status,
                 "ok": bool(ok), "ms": round(ms, 1), "req": "", "resp": "", "error": err}
        _seq[0] += 1
        with open(_CALLS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# --- payment network capture (for the replay-profile classifier) --------------
# Match any request that looks payment-related: hosted-gateway hosts OR a path that
# smells like a payment/tokenization/3DS endpoint. Platform-agnostic on purpose.
_PAY_HOSTS_RE = re.compile(
    r"(stripe|cybersource|secureacceptance|flex\.cybersource|adyen|checkoutshopper|"
    r"braintree|paypal|checkout\.com|worldpay|klarna|square|razorpay|payment|paradox|"
    r"3ds|threeds|/acs|tokeniz|/pay(?:ment)?s?\b|checkout_update|getparams)", re.I)
# Card-number-like run of 13-19 digits (with optional space/dash separators).
_PAN_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _redact(s):
    """Strip PANs and obvious secret fields BEFORE anything is written to disk.
    Public sandbox test cards only ever reach here, but we redact regardless so a
    real PAN can never be persisted (PCI). Also caps size."""
    if not s:
        return s
    s = str(s)
    if len(s) > 2000:
        s = s[:2000] + "...(truncated)"
    s = _PAN_RE.sub("[REDACTED_PAN]", s)
    s = re.sub(r'((?:card(?:number)?|cc(?:num)?|pan|cardnumber|cvv|cvc|cvn|'
               r'securitycode|signature|secret|apikey|api_key|token|password)'
               r'\W{0,3}["\':=]\s*)[^",&}\s]+', r'\1[REDACTED]', s, flags=re.I)
    return s


def _redact_headers(h):
    out = {}
    try:
        for k, v in (h or {}).items():
            out[k] = "[REDACTED]" if k.lower() in (
                "authorization", "cookie", "set-cookie", "x-api-key", "api-key",
                "proxy-authorization") else v
    except Exception:
        pass
    return out


def _attach_payment_capture(page):
    """Record the payment gateway/iframe request sequence (URL, method, headers,
    redacted body) into _payreqs. Best-effort and fully guarded: any failure here
    must never affect the load run."""
    def _on_req(req):
        try:
            url = getattr(req, "url", "") or ""
            if not _PAY_HOSTS_RE.search(url) or len(_payreqs) >= 60:
                return
            try:
                body = req.post_data
            except Exception:
                body = None
            _payreqs.append({
                "phase": "request", "method": getattr(req, "method", ""),
                "url": url[:300], "resource_type": getattr(req, "resource_type", ""),
                "headers": _redact_headers(getattr(req, "headers", {}) or {}),
                "post_data": _redact(body), "status": None,
                "ts": round(time.time(), 3)})
        except Exception:
            pass

    def _on_resp(resp):
        try:
            url = getattr(resp, "url", "") or ""
            if not _PAY_HOSTS_RE.search(url):
                return
            for e in reversed(_payreqs):
                if e.get("url") == url[:300] and e.get("status") is None:
                    e["status"] = getattr(resp, "status", None)
                    break
        except Exception:
            pass

    try:
        page.on("request", _on_req)
        page.on("response", _on_resp)
    except Exception:
        pass


def _abs(host, path):
    if not path:
        return host
    if path.startswith("http"):
        return path
    if not host:
        return path
    return host.rstrip("/") + "/" + path.lstrip("/")


def _pct(vals, q):
    if not vals:
        return 0
    s = sorted(vals)
    k = min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1))))
    return s[k]


def _write_summary(note=""):
    try:
        os.makedirs(_RESULTS, exist_ok=True)
        total = sum(len(v) for v in _samples.values())
        fails = sum(_fails.values())
        # Fail-closed: if a card payment was REQUIRED but did not clear, say so
        # explicitly (status=payment_failed) instead of "ok", so the executor
        # fails the whole run — never mask an uncleared card as success.
        status = "ok"
        if _pay["required"] and not _pay["ok"]:
            status = "payment_failed"
            note = "card payment did not clear: " + (_pay["err"] or "card form not reachable")
        json.dump({"track": "browser", "iterations": _iters[0],
                   "requests": total, "failures": fails, "note": note,
                   "status": status,
                   "payment_required": _pay["required"], "payment_ok": _pay["ok"],
                   "payment_err": _pay["err"], "gateway": _pay["gateway"],
                   "prices": _prices[:50],
                   "payment_network": _payreqs[:60],
                   "order_confirmation": (_order_confirm or None)},
                  open(_SUMMARY, "w", encoding="utf-8"))
    except Exception:
        pass


def _write_stats():
    try:
        os.makedirs(_RESULTS, exist_ok=True)
        cols = ["Type", "Name", "Request Count", "Failure Count",
                "Median Response Time", "Average Response Time",
                "Min Response Time", "Max Response Time",
                "50%", "90%", "95%", "99%", "Requests/s"]
        import csv as _csv
        with open(_STATS, "w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            w.writerow(cols)
            allv = []
            for label, vals in _samples.items():
                allv += vals
                w.writerow(_row("GET", label, vals, _fails.get(label, 0)))
            w.writerow(_row("", "Aggregated", allv, sum(_fails.values())))
    except Exception:
        pass


def _row(method, name, vals, fails):
    n = len(vals)
    avg = round(statistics.mean(vals), 1) if vals else 0
    return [method, name, n, fails,
            round(_pct(vals, 50), 1), avg,
            round(min(vals), 1) if vals else 0, round(max(vals), 1) if vals else 0,
            round(_pct(vals, 50), 1), round(_pct(vals, 90), 1),
            round(_pct(vals, 95), 1), round(_pct(vals, 99), 1), 0]


async def _fill_in_frames(frames, selectors, value):
    """Fill the first matching selector found in any of the given frames. Uses
    click + type so Stripe's field JS fires (fill alone can be ignored)."""
    for sel in selectors:
        if not sel:
            continue
        for f in frames:
            try:
                el = await f.query_selector(sel)
                if el:
                    await el.click()
                    try:
                        await el.fill("")
                    except Exception:
                        pass
                    await el.type(str(value), delay=25)
                    return True
            except Exception:
                continue
    return False


async def _capture_price(page):
    """Read the first real currency amount rendered on the page (the price HTTP
    replay reports as 0 when it's computed client-side). Records it once per
    distinct url+value. Best-effort, never raises."""
    try:
        for sel in _PRICE_SELECTORS:
            try:
                els = await page.query_selector_all(sel)
            except Exception:
                continue
            for el in els[:25]:
                try:
                    txt = (await el.inner_text()) or ""
                except Exception:
                    continue
                m = _PRICE_RE.search(txt)
                if m:
                    val = m.group(0).strip()
                    url = ""
                    try:
                        url = page.url
                    except Exception:
                        pass
                    if not any(p["url"] == url and p["price"] == val for p in _prices):
                        if len(_prices) < 50:
                            _prices.append({"url": url, "price": val})
                        _log_call("Product price (browser)", "PRICE", url, 200, True, 0, "")
                    return val
        # Fallback (Case B): price only in a JS blob, not rendered text — read the
        # common client-side product-state objects the storefront populated.
        try:
            js = await page.evaluate(
                "() => { const p = (window.product||{}); "
                "const s = (window.__INITIAL_STATE__&&window.__INITIAL_STATE__.product)||{}; "
                "const d = (Array.isArray(window.dataLayer)?window.dataLayer:[]).find(x=>x&&(x.price||x.value))||{}; "
                "return String(p.price||s.price||d.price||d.value||''); }")
            if js:
                m2 = re.search(r"[0-9][0-9,]*\.?[0-9]*", str(js))
                if m2 and float(m2.group(0).replace(",", "")) > 0:
                    val = m2.group(0)
                    url = ""
                    try:
                        url = page.url
                    except Exception:
                        pass
                    if not any(p["url"] == url and p["price"] == val for p in _prices):
                        if len(_prices) < 50:
                            _prices.append({"url": url, "price": val, "source": "js-state"})
                        _log_call("Product price (browser, JS state)", "PRICE", url, 200, True, 0, "")
                    return val
        except Exception:
            pass
    except Exception:
        pass
    return None


async def _safe_url(page):
    try:
        return page.url
    except Exception:
        return ""


async def _click_any(page, selectors, texts, timeout=5000):
    """Click the first matching CSS selector, else the first button/link whose
    visible text matches. Returns True on a click."""
    for sel in (selectors or []):
        if not sel:
            continue
        try:
            await page.click(sel, timeout=timeout)
            return True
        except Exception:
            continue
    for txt in (texts or []):
        for role in ("button", "link"):
            try:
                await page.get_by_role(role, name=re.compile(re.escape(txt), re.I)).first.click(timeout=timeout)
                return True
            except Exception:
                continue
    return False


async def _add_to_cart(page, sel=None):
    """Put a product in the browser's OWN cart so checkout has something to pay
    for (the HTTP track's REST cart is a different session). Best-effort."""
    label = "Add to cart (browser)"
    t0 = time.time(); ok = False; err = ""
    try:
        ok = await _click_any(page, ([sel] if sel else []) + _ADDCART_SELECTORS,
                              _ADDCART_TEXTS, timeout=8000)
        if ok:
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
        else:
            err = ("no add-to-cart control found on the product page — the run may "
                   "not be on a simple in-stock PDP (configurable options?)")
    except Exception as e:
        ok = False; err = str(e)[:200]
    ms = (time.time() - t0) * 1000.0
    _record(label, ms, ok)
    _log_call(label, "CART", await _safe_url(page), 200 if ok else 0, ok, ms, err)
    return ok


async def _advance_checkout(page, payment_method_selector=None):
    """Best-effort walk of a multi-step checkout: pick the first shipping method,
    click through Next/Continue, then select the CARD payment method so its hosted
    iframe renders. Site-variable — heuristic, guarded, never raises."""
    try:
        for _ in range(3):
            try:
                radios = page.locator("input[type=radio]")
                if await radios.count() > 0:
                    await radios.first.check(timeout=2000)     # a shipping method
            except Exception:
                pass
            if not await _click_any(page, [], _NEXT_TEXTS, timeout=4000):
                break
            await asyncio.sleep(1.5)
        # select the card payment method (this is what reveals the gateway iframe)
        await _click_any(page, ([payment_method_selector] if payment_method_selector else [])
                         + _PAY_RADIO_SELECTORS, [], timeout=4000)
        await asyncio.sleep(1.5)
    except Exception:
        pass


async def _click_submit(page, submit_selector):
    """Click the Pay / Place-order button. Uses the supplied/inferred selector
    first, then platform-agnostic selector + visible-text heuristics."""
    if submit_selector:
        try:
            await page.click(submit_selector, timeout=20000)
            return True
        except Exception:
            pass
    for sel in _SUBMIT_SELECTORS:
        try:
            await page.click(sel, timeout=3500)
            return True
        except Exception:
            continue
    for txt in _SUBMIT_TEXTS:
        try:
            await page.get_by_role("button", name=re.compile(re.escape(txt), re.I)).first.click(timeout=3500)
            return True
        except Exception:
            continue
    return False


def _rec_value(role, card, recorded):
    """Resolve the value to enter for a recorded field: card fields come from the
    SANDBOX test card (never the recorded/real card); billing fields reuse the
    recorded value."""
    exp = str(card.get("exp", "12 / 34"))
    mm = exp.split("/")[0].strip() if "/" in exp else "12"
    yy = exp.split("/")[1].strip() if "/" in exp else "34"
    table = {"number": card.get("number", "4111111111111111"),
             "cvc": card.get("cvc", "123"), "exp_month": mm, "exp_year": yy,
             "expiry": exp, "postal": card.get("postal", "12345")}
    if role in table:
        return table[role]
    return recorded            # billing: recorded value, or None -> skip


def _year_variants(v):
    """Try both 2- and 4-digit forms of an expiry year, since hosted card forms
    use one or the other (e.g. sandbox card '34' vs a <select> of '2034')."""
    s = str(v).strip()
    out = [s]
    if len(s) == 2 and s.isdigit():
        out.append("20" + s)
    elif len(s) == 4 and s.isdigit():
        out.append(s[2:])
    return out


async def _fill_loc(ctx, sel, val, action):
    """Fill/select one field in a given context (a frame_locator or the page),
    guarded by count() so an absent selector is skipped INSTANTLY instead of
    burning a multi-second timeout (the old bug that made a missing card form
    take minutes). Returns True on success."""
    try:
        loc = ctx.locator(sel).first
        if await loc.count() == 0:
            return False
        if action == "select":
            for v in _year_variants(val):
                try:
                    await loc.select_option(value=str(v), timeout=3000)
                    return True
                except Exception:
                    pass
            try:
                await loc.select_option(label=str(val), timeout=3000)
                return True
            except Exception:
                return False
        try:
            await loc.click(timeout=3000)
        except Exception:
            pass
        await loc.fill(str(val), timeout=3000)
        return True
    except Exception:
        return False


async def _drive_recorded_payment(page, payment):
    """Replay the EXACT payment flow captured in the recording (real iframe +
    field selectors), driven by the SANDBOX test card. Returns the number of card
    fields filled (0 => caller falls back to generic KB selectors, then fails
    closed). Never raises.

    Robustness: after revealing the form it WAITS for the card-number field to
    actually attach inside the hosted iframe (bounded) before filling — so a form
    that never rendered fails in ~15s, not minutes, and card fields are only typed
    once the frame is genuinely present."""
    steps = payment.get("recorded_steps") or {}
    fields = steps.get("fields") or []
    if not fields:
        return 0
    card = payment.get("card") or {}
    try:
        # 1) Reveal the payment form (e.g. the recorded "Proceed To Payment").
        if steps.get("pay_trigger"):
            try:
                await page.click(steps["pay_trigger"], timeout=8000)
            except Exception:
                pass
        # 2) Locate the card context and WAIT (bounded) for the number field to
        #    render — card fields live inside the recorded iframe; some stores put
        #    them on the main page (fl stays None then).
        number_sel = next((f.get("sel") for f in fields if f.get("role") == "number"), None)
        fl = page.frame_locator(steps["iframe"]) if steps.get("iframe") else None
        ready = False
        if number_sel:
            for _ in range(15):                       # ~15s, checked every 1s
                try:
                    if fl is not None and await fl.locator(number_sel).count() > 0:
                        ready = True
                        break
                    if await page.locator(number_sel).count() > 0:
                        fl = None                     # fields are on the main page
                        ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)
        else:
            ready = True                              # no card number to gate on
        if number_sel and not ready:
            try:
                _frs = " | ".join((f.url or "?")[:55] for f in page.frames)
            except Exception:
                _frs = "?"
            _log_call("Payment: card form", "PAY", "", 0, False, 0,
                      "card form never rendered (Proceed-to-Payment not reached / iframe "
                      "not loaded). frames=[%s]" % _frs[:280])
            return 0                                  # form never appeared — fail fast
        _log_call("Payment: card form", "PAY", "", 200, True, 0,
                  "card form ready (%s)" % ("iframe" if fl is not None else "main page"))
        # 3) Fill the fields. Card fields go in the iframe (fall back to page);
        #    login/identity fields are skipped (already authenticated).
        filled = 0
        for f in fields:
            role = f.get("role")
            if role in ("email", "username", "password"):
                continue
            val = _rec_value(role, card, f.get("value"))
            if val in (None, ""):
                continue
            for ctx in ([fl, page] if fl is not None else [page]):
                if ctx is None:
                    continue
                if await _fill_loc(ctx, f.get("sel"), val, f.get("action")):
                    filled += 1
                    break
        _log_call("Payment: card fields", "PAY", "", 200 if filled else 0, bool(filled), 0,
                  "%d card/billing field(s) filled" % filled)
        # 4) Accept the T&C agreement (main page), if the recording captured one.
        if steps.get("agreement"):
            for how in ("check", "click"):
                try:
                    await getattr(page, how)(steps["agreement"], timeout=3000)
                    break
                except Exception:
                    continue
        # 5) Submit the card WITHIN the hosted iframe. Secure Acceptance / hosted
        #    forms tokenize + post back via their OWN in-iframe submit ("commit")
        #    button — the outer-page Place-order can't reach it, so a card that was
        #    typed but never committed would never actually clear.
        if fl is not None and filled:
            for csel in ('[name="commit"]', 'input[type="submit"]',
                         'button[type="submit"]', 'button:has-text("Pay")',
                         'button:has-text("Continue")', 'button:has-text("Submit")'):
                try:
                    loc = fl.locator(csel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=4000)
                        _log_call("Payment: iframe submit", "PAY", "", 200, True, 0,
                                  "clicked in-iframe %s" % csel)
                        break
                except Exception:
                    continue
        return filled
    except Exception:
        return 0


async def _drive_payment(page, payment, host=""):
    """Drive a real hosted-iframe card payment with a SANDBOX test card and report
    an HONEST outcome. Fail-closed: card-field-not-found, a hang, an un-clickable
    Pay button, or a configured success URL that is never reached ALL count as
    FAILURE (recorded in _pay so the run is failed) — the card is never quietly
    skipped and no other payment method is substituted. Bounded by a hard time
    budget so a missing card form fails in seconds, not minutes. Never uses live
    card data — only the KB's public sandbox test cards."""
    if not payment or not payment.get("gateway"):
        return
    gateway = str(payment.get("gateway"))
    _pay["required"] = True
    _pay["gateway"] = gateway
    label = "Payment (%s)" % gateway
    t0 = time.time(); ok = False; err = ""
    try:
        # Hard budget: a missing/mismatched card form must fail fast, never hang
        # for minutes as it did before (the run was falsely reported "ok").
        await asyncio.wait_for(_drive_payment_inner(page, payment, host, gateway),
                               timeout=75)
        ok = True
    except asyncio.TimeoutError:
        err = ("card payment exceeded its 75s budget — the hosted %s card form was "
               "not reachable (selectors did not match the real form?)" % gateway)
    except Exception as e:
        err = str(e)[:200]
    if ok:
        _pay["ok"] = True
    else:
        _pay["err"] = err or _pay["err"] or "unknown payment failure"
    ms = (time.time() - t0) * 1000.0
    _record(label, ms, ok)
    try:
        url = page.url
    except Exception:
        url = ""
    _log_call(label, "PAY", url, 200 if ok else 0, ok, ms, err)


async def _drive_payment_inner(page, payment, host, gateway):
    """Card entry + submit + order confirmation. RAISES on any failure so the
    caller records a failed payment (fail-closed). Gateway-agnostic: driven by the
    frame patterns / field selectors resolved from browser_patterns.yaml."""
    if payment.get("url"):
        try:
            await page.goto(_abs(host, payment["url"]),
                            wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
    if payment.get("pay_trigger_selector"):
        try:
            await page.click(payment["pay_trigger_selector"], timeout=8000)
        except Exception:
            pass
    await asyncio.sleep(1.5)   # let the gateway's hosted iframe(s) attach

    card = payment.get("card") or {}
    # 1) FAITHFUL replay — exact recorded iframe + field selectors (best fidelity).
    used_recorded = False
    if payment.get("recorded_steps"):
        used_recorded = bool(await _drive_recorded_payment(page, payment))
    # 2) FALLBACK — generic KB frame patterns + selectors (Stripe/CyberSource…).
    if not used_recorded:
        pats = payment.get("frame_url_patterns") or ["js.stripe.com"]
        frames = [f for f in page.frames if any(p in (f.url or "") for p in pats)]
        if not frames:
            frames = [f for f in page.frames
                      if any(h in (f.url or "") for h in _KNOWN_GATEWAY_HOSTS)]
        pe = payment.get("payment_element_fields") or {}
        ce = payment.get("card_element_fields") or {}
        if not await _fill_in_frames(frames, [pe.get("number"), ce.get("number")],
                                     card.get("number", "4242424242424242")):
            raise RuntimeError("%s card field not found — the recording had no "
                               "captured payment steps and the KB selectors didn't "
                               "match the real hosted form (did the run reach the "
                               "payment step?)." % gateway)
        await _fill_in_frames(frames, [pe.get("expiry"), ce.get("expiry")], card.get("exp", "12 / 34"))
        await _fill_in_frames(frames, [pe.get("cvc"), ce.get("cvc")], card.get("cvc", "123"))
        await _fill_in_frames(frames, [pe.get("postal"), ce.get("postal")], card.get("postal", "12345"))

    _submit = (payment.get("recorded_steps") or {}).get("submit") or payment.get("submit_selector")
    if not await _click_submit(page, _submit):
        raise RuntimeError("could not click the Pay / Place-order button after "
                           "entering the card")

    # TEST-mode 3DS / challenge frame (if the scenario or store config triggers it).
    tds = payment.get("threeds") or {}
    tds_pats = tds.get("frame_url_patterns") or ["3ds", "hooks.stripe.com", "acs"]
    for sel in (tds.get("complete_selectors") or []):
        clicked = False
        try:
            await asyncio.sleep(1.5)
            tframes = [f for f in page.frames if any(p in (f.url or "") for p in tds_pats)]
            for f in tframes:
                try:
                    el = await f.query_selector(sel)
                    if el:
                        await el.click(); clicked = True; break
                except Exception:
                    continue
        except Exception:
            pass
        if clicked:
            break

    # Confirm the order actually placed. When a success signal is configured we
    # REQUIRE it (fail-closed): reaching it is the proof the card cleared. This
    # wait_for_url RAISES on timeout, which the caller records as a failure.
    succ = payment.get("success_url_contains")
    if succ:
        await page.wait_for_url("**" + succ + "**", timeout=20000)


async def _fill_first(page, selectors, value):
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(value)
                return True
        except Exception:
            continue
    return False


async def _login(page, host, login):
    """Log in, platform-agnostically. Uses planner-supplied url/selectors when
    present; otherwise tries common login paths and field/button heuristics so it
    works on an arbitrary app. No-op without credentials. Never raises."""
    if not login:
        return
    user, pw = login.get("username"), login.get("password")
    if not (user or pw):
        return
    urls = [login["url"]] if login.get("url") else _LOGIN_PATHS
    for u in urls:
        try:
            await page.goto(_abs(host, u), wait_until="domcontentloaded", timeout=30000)
        except Exception:
            continue
        oku = okp = False
        if login.get("username_selector") and user:
            try:
                await page.fill(login["username_selector"], user); oku = True
            except Exception:
                pass
        if not oku and user:
            oku = await _fill_first(page, _USER_SELECTORS, user)
        if login.get("password_selector") and pw:
            try:
                await page.fill(login["password_selector"], pw); okp = True
            except Exception:
                pass
        if not okp and pw:
            okp = await _fill_first(page, _PASS_SELECTORS, pw)
        if not (oku or okp):
            continue                       # no login form on this page — try next
        if login.get("submit_selector"):
            try:
                await page.click(login["submit_selector"])
            except Exception:
                pass
        else:
            for txt in _LOGIN_SUBMIT_TEXTS:
                try:
                    await page.get_by_role("button", name=re.compile(re.escape(txt), re.I)).first.click(timeout=3000)
                    break
                except Exception:
                    continue
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        return


async def _settle(page):
    """Give a JS-rendered (SPA) page time to hydrate/navigate before the next
    action, so recorded elements are actually present + interactive."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass


async def _try_click(ctx, sel, fast=False):
    """One click attempt. The PRIMARY recorded selector (fast=False) auto-waits 5s
    so an SPA element that mounts a moment later is still found. FALLBACK selectors
    (fast=True) count()-check first and use a short timeout, so trying many KB
    fallbacks can't stack into 40-50s. count() also guards the JS-click fallback."""
    try:
        loc = ctx.locator(sel).first
        if fast and await loc.count() == 0:          # fallback + absent -> skip instantly
            return False
        try:
            await loc.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            await loc.click(timeout=(2500 if fast else 5000))
            return True
        except Exception:
            if await loc.count() == 0:
                return False
            await loc.evaluate("el => el.click()")
            return True
    except Exception:
        return False


async def _robust_click(ctx, sel, page=None, intent=None, resilience=None):
    """Click resiliently. Try the recorded selector; on failure, fall back to the
    KB resilience patterns for this click's INTENT (product / add_to_cart /
    checkout / proceed_payment / place_order / signin) — generic across stores."""
    if await _try_click(ctx, sel):                   # primary recorded selector (auto-waits)
        return True
    r = ((resilience or {}).get("intents") or {}).get(intent or "") or {}
    for fsel in r.get("selectors", []):              # KB fallbacks (fast-fail, no stacking)
        if await _try_click(ctx, fsel, fast=True):
            return True
        if page is not None and page is not ctx and await _try_click(page, fsel, fast=True):
            return True
    for txt in r.get("texts", []):
        for role in ("button", "link"):
            try:
                loc = (page or ctx).get_by_role(role, name=re.compile(re.escape(txt), re.I)).first
                if await loc.count() == 0:        # fast skip when text absent (no 3.5s stack)
                    continue
                await loc.click(timeout=2500)
                return True
            except Exception:
                continue
    return False


async def _robust_fill(ctx, sel, value):
    """Fill resiliently. The normal fill AUTO-WAITS (5s) so an SPA field that
    mounts a moment later is still found (this is what makes login work). The
    count() guard is used ONLY before the JS fallback to avoid a 30s hang."""
    try:
        loc = ctx.locator(sel).first
        try:
            await loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            await loc.fill(str(value), timeout=5000)     # auto-waits for SPA render
            return True
        except Exception:
            if await loc.count() == 0:
                return False
            await loc.evaluate(
                "(el, v) => { el.value = v;"
                " el.dispatchEvent(new Event('input', {bubbles:true}));"
                " el.dispatchEvent(new Event('change', {bubbles:true})); }", str(value))
            return True
    except Exception:
        return False


def _resolve_action_value(role, recorded, creds, card):
    """Value to enter for a replayed field: card fields -> sandbox test card,
    credentials -> the run's creds, everything else -> the recorded value."""
    if role in ("number", "cvc", "exp_month", "exp_year", "expiry", "postal"):
        return _rec_value(role, card, recorded)
    if role == "password":
        return (creds or {}).get("password") or recorded
    if role in ("username", "email"):
        return (creds or {}).get("username") or recorded
    return recorded


async def _replay_ui_journey(page, actions, host, creds, card, deadline, resilience=None):
    """Replay the recorded browser journey verbatim (navigate / click / type /
    select / switch-iframe), fast-failing each step so a mismatch costs ~seconds,
    not minutes. On a failed click it falls back to the KB resilience patterns for
    that step's intent. This reproduces the WHOLE business flow for any recording."""
    frame = None
    if actions and actions[0].get("action") != "navigate":
        try:
            await page.goto(_abs(host, "/"), wait_until="domcontentloaded", timeout=30000)
            await _settle(page)
        except Exception:
            pass
    for a in actions:
        if time.time() >= deadline:
            break
        act = a.get("action"); t0 = time.time(); ok = True; err = ""; name = "UI " + str(act)
        try:
            if act == "navigate":
                await page.goto(_abs(host, a.get("url")), wait_until="domcontentloaded", timeout=30000)
                await _settle(page)                 # let the SPA hydrate
                frame = None; name = "UI navigate"
            elif act == "frame":
                frame = page.frame_locator(a["sel"]); name = "UI enter-iframe"
            elif act == "frame_reset":
                frame = None; name = "UI leave-iframe"
            elif act == "click":
                ok = await _robust_click(frame or page, a["sel"], page=page,
                                         intent=a.get("intent"), resilience=resilience)
                name = "UI click %s" % (a.get("intent") or "")
                if not ok:
                    err = "click target not found/clickable: %s (intent=%s)" % (a["sel"], a.get("intent"))
                else:
                    await _settle(page)             # a click may navigate the SPA
            elif act == "type":
                val = _resolve_action_value(a.get("role"), a.get("value"), creds, card)
                if val in (None, ""):
                    raise RuntimeError("no value for role %s" % a.get("role"))
                ok = await _robust_fill(frame or page, a["sel"], val)
                name = "UI type %s" % (a.get("role") or "")
                if not ok:
                    err = "field not found: %s" % a["sel"]
            elif act == "select":
                val = _resolve_action_value(a.get("role"), a.get("value"), creds, card)
                loc = (frame or page).locator(a["sel"]).first
                try:
                    await loc.select_option(value=str(val), timeout=5000)
                except Exception:
                    await loc.select_option(label=str(val), timeout=5000)
                name = "UI select %s" % (a.get("role") or "")
        except Exception as e:
            ok = False; err = str(e)[:150]
        ms = (time.time() - t0) * 1000.0
        _record(name, ms, ok)
        _log_call(name, str(act).upper(), await _safe_url(page), 200 if ok else 0, ok, ms, err)
        if act in ("navigate", "click"):
            await _capture_price(page)          # opportunistic real-price read
        await asyncio.sleep(0.2)


async def _cart_count(page):
    """Best-effort read of the minicart item count, to confirm add-to-cart really
    populated the cart — a successful CLICK does NOT prove an item was added (a
    configurable product, for instance, needs options first). Returns a string
    ('0','1',…) or '?' when the counter can't be read. Never raises."""
    for sel in [".counter-number", ".minicart-wrapper .counter.qty .counter-number",
                "[data-block='minicart'] .counter-number",
                ".action.showcart .counter-number", ".counter.qty .counter-number"]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                txt = ((await loc.inner_text()) or "").strip()
                if txt != "":
                    return txt
        except Exception:
            continue
    return "?"


async def _build_cart_direct(page, host, product_search, resilience):
    """Reach a REAL product robustly: open the store's search results for the
    recorded term, read the FIRST product link's href, navigate straight to that
    PDP (no fragile tile-click), capture its real price, then add to cart via KB
    selectors. Returns True if add-to-cart succeeded. Never raises."""
    term = (product_search or {}).get("term")
    surl = (product_search or {}).get("results_url")
    if not (term or surl):
        return False
    try:
        await page.goto(_abs(host, surl or ("/catalogsearch/result/?q=" + str(term))),
                        wait_until="domcontentloaded", timeout=30000)
        await _settle(page)
    except Exception:
        pass
    sels = (((resilience or {}).get("intents") or {}).get("product") or {}).get("selectors") or []
    href = None
    for s in sels:
        try:
            loc = page.locator(s).first
            if await loc.count() > 0:
                href = await loc.get_attribute("href")
                if href:
                    break
        except Exception:
            continue
    t0 = time.time(); ok = False; err = ""
    if href:
        try:
            await page.goto(_abs(host, href), wait_until="domcontentloaded", timeout=30000)
            await _settle(page)
            await _capture_price(page)          # real PDP price
            ok = True
        except Exception as e:
            err = str(e)[:150]
    else:
        err = "no product link on search results for '%s'" % term
    _record("Reach PDP (browser)", (time.time() - t0) * 1000.0, ok)
    _log_call("Reach PDP (browser)", "NAV", await _safe_url(page), 200 if ok else 0, ok,
              (time.time() - t0) * 1000.0, err)
    added = await _robust_click(page, "#product-addtocart-button", page=page,
                                intent="add_to_cart", resilience=resilience)
    if added:
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    cnt = await _cart_count(page)                    # did the item REALLY land in the cart?
    really = added and cnt not in ("0", "?")
    _record("Add to cart (browser)", 0, really)
    _log_call("Add to cart (browser)", "CART", await _safe_url(page), 200 if really else 0, really, 0,
              ("minicart qty=%s" % cnt) if added
              else "add-to-cart control not found on PDP")
    return really


async def _confirm_order_placed(page):
    """Fail-closed proof the card actually cleared: a real order-confirmation
    signal — a success URL or thank-you / order-number text. Returns True only on
    genuine confirmation, so a card that was entered but never placed is NOT
    reported as success. Never raises."""
    sigs = ["onepage/success", "checkout/success", "order-received", "thank-you",
            "thankyou", "order-confirmation", "/success", "checkout/onepage/success"]

    def _note(kind, value, url=None, number=None):
        # Record the FIRST signal that confirmed the order — the crawl-derived
        # order-success assertion. Best-effort, never raises.
        try:
            if not _order_confirm.get("confirmed"):
                _order_confirm.update({"confirmed": True, "signal_type": kind,
                                       "signal": value})
                if url:
                    _order_confirm["url"] = str(url)[:200]
                if number:
                    _order_confirm["order_number"] = str(number)[:64]
        except Exception:
            pass

    try:
        _u = (page.url or "").lower()
        for s in sigs:
            if s in _u:
                _note("url", s, url=page.url)
                return True
    except Exception:
        pass
    for s in sigs:                                    # brief wait for a redirect
        try:
            await page.wait_for_url("**" + s + "**", timeout=3000)
            _note("url", s, url=(page.url or ""))
            return True
        except Exception:
            continue
    try:
        low = ((await page.inner_text("body")) or "").lower()
        for k in ("thank you for your order", "your order number", "order number",
                  "order has been received", "order confirmation"):
            if k in low:
                m = re.search(r'order\s*(?:number|no\.?|#)\s*[:#]?\s*([a-z0-9\-]{3,})', low)
                _note("phrase", k, number=(m.group(1) if m else None))
                return True
    except Exception:
        pass
    return False


async def _run_vu(browser, journey, deadline):
    host = journey.get("host") or ""
    steps = journey.get("steps") or []
    ui_journey = journey.get("ui_journey") or []
    product_search = journey.get("product_search") or {}
    resilience = journey.get("resilience") or {}
    checkout_url = journey.get("checkout_url")
    payment = journey.get("payment") or {}
    login = journey.get("login") or {}
    card = payment.get("card") or {}
    try:
        ctx = await browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()
        _attach_payment_capture(page)   # record payment traffic for the classifier
    except Exception:
        return
    while time.time() < deadline:
        _iters[0] += 1
        # PROVEN FIRST — if the recording captured the CARD-entry steps, replay the
        # full recorded journey (browse -> cart -> minicart -> checkout-link ->
        # checkout login -> Proceed To Payment -> card -> in-iframe commit ->
        # agreement -> cc-cid). This is the human-verified flow; the heuristic
        # direct path kept failing to reach the payment step because it skips the
        # recorded minicart / checkout-link / checkout-login steps.
        if ui_journey and any(a.get("role") == "number" for a in ui_journey):
            has_login = any(a.get("role") in ("username", "password", "email") for a in ui_journey)
            if not has_login:
                await _login(page, host, login)
            await _replay_ui_journey(page, ui_journey, host, login, card, deadline,
                                     resilience=journey.get("resilience"))
            # Robust card entry: the replay's fast-fail clicks give up before the
            # hosted iframe finishes rendering (a few seconds after Proceed-To-
            # Payment), so re-drive the card with the waiting/select-aware driver.
            await _drive_recorded_payment(page, payment)
            # place the order (the recording may not capture the final submit)
            await _click_submit(page, (payment.get("recorded_steps") or {}).get("submit"))
            # FAIL-CLOSED confirmation: only mark the card cleared on a REAL order
            # confirmation; otherwise _pay stays required-but-not-ok -> run fails.
            gw = payment.get("gateway")
            if gw:
                if await _confirm_order_placed(page):
                    _pay["ok"] = True
                    _log_call("Payment (%s)" % gw, "PAY", await _safe_url(page), 200, True, 0,
                              "order confirmed (recorded replay)")
                else:
                    _pay["err"] = (_pay["err"] or "card entered via recorded replay but no "
                                   "order-confirmation signal was seen")
                    _log_call("Payment (%s)" % gw, "PAY", await _safe_url(page), 0, False, 0,
                              _pay["err"])
            continue
        # PREFERRED (no recorded card steps): log in, reach a REAL product via
        # search-results href (robust), add to cart, checkout, then drive the card
        # using the recorded payment selectors.
        if payment and product_search.get("term"):
            await _login(page, host, login)
            added = await _build_cart_direct(page, host, product_search, resilience)
            if checkout_url:
                try:
                    await page.goto(_abs(host, checkout_url),
                                    wait_until="domcontentloaded", timeout=30000)
                    await _settle(page)
                except Exception:
                    pass
                # Diagnostic: WHERE did checkout land? Magento redirects /checkout ->
                # /checkout/cart when the cart is empty, so this pinpoints an
                # empty-cart problem vs a not-reaching-payment problem.
                _cu = await _safe_url(page)
                _on_checkout = ("checkout" in _cu.lower() and "cart" not in _cu.lower().rsplit("/", 1)[-1])
                _log_call("Reach checkout (browser)", "NAV", _cu, 200 if _on_checkout else 0,
                          _on_checkout, 0,
                          "" if _on_checkout else "redirected off checkout (empty cart / not logged in?)")
            await _advance_checkout(page, journey.get("payment_method_selector"))
            await _drive_payment(page, payment, host)
            continue
        # replay the recorded browser journey (has a journey but no card steps).
        if ui_journey:
            has_login = any(a.get("role") in ("username", "password", "email") for a in ui_journey)
            if not has_login:
                await _login(page, host, login)     # recording assumed a session
            await _replay_ui_journey(page, ui_journey, host, login, card, deadline,
                                     resilience=journey.get("resilience"))
            await _click_submit(page, (payment.get("recorded_steps") or {}).get("submit"))
            continue
        # FALLBACK (recording had no Selenium steps): scaffolded nav + heuristics.
        await _login(page, host, login)
        for step in steps:
            if time.time() >= deadline:
                break
            url = _abs(host, step.get("path"))
            label = step.get("label") or url
            t0 = time.time(); ok = True; status = 0; err = ""
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                status = resp.status if resp else 0
                ok = (status < 400) if status else True
                if not ok:
                    err = "HTTP %s" % status
            except Exception as e:
                ok = False; err = str(e)[:200]
            ms = (time.time() - t0) * 1000.0
            _record(label, ms, ok)
            _log_call(label, "GET", url, status, ok, ms, err)
            if ok:
                await _capture_price(page)
            await asyncio.sleep(random.uniform(1.0, 3.0))
        if payment:
            prod = journey.get("product_url")
            if prod:
                try:
                    await page.goto(_abs(host, prod), wait_until="domcontentloaded", timeout=30000)
                    await _capture_price(page)
                except Exception:
                    pass
            await _add_to_cart(page, journey.get("add_to_cart_selector"))
            if journey.get("checkout_url"):
                try:
                    await page.goto(_abs(host, journey["checkout_url"]),
                                    wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
            await _advance_checkout(page, journey.get("payment_method_selector"))
            await _drive_payment(page, payment, host)
    try:
        await ctx.close()
    except Exception:
        pass


async def _delayed(browser, journey, deadline, idx, vus, ramp):
    await asyncio.sleep((idx / max(1, vus)) * ramp)
    await _run_vu(browser, journey, deadline)


async def _main(args):
    try:
        journey = json.load(open(_JOURNEY, encoding="utf-8"))
    except Exception as e:
        _write_summary("no journey.json: %s" % e); return
    # Mark payment required up-front from the journey, so a card that is never even
    # reached (e.g. add-to-cart failed) still fails-closed instead of reporting ok.
    try:
        _gw = (journey.get("payment") or {}).get("gateway")
        if _gw:
            _pay["required"] = True
            _pay["gateway"] = _gw
    except Exception:
        pass
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        _write_summary("Playwright not installed (pip install playwright): %s" % e); return
    deadline = time.time() + args.duration
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:
                _write_summary("browser launch failed (run: playwright install chromium): %s" % e)
                return
            tasks = [asyncio.create_task(_delayed(browser, journey, deadline, i, args.vus, args.ramp))
                     for i in range(max(1, args.vus))]
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await browser.close()
            except Exception:
                pass
    except Exception as e:
        _write_summary("runner error: %s" % e)
        _write_stats()
        return
    _write_stats()
    _write_summary("ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="")
    ap.add_argument("--vus", type=int, default=5)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--ramp", type=int, default=5)
    a = ap.parse_args()
    try:
        asyncio.run(_main(a))
    except Exception as e:
        _write_summary("fatal: %s" % e)
'''
