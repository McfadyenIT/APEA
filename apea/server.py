"""APEA orchestrator — FastAPI backend + web UI.

Routes non-technical UI requests to the specialized sub-agents:
    /api/discover -> Discovery agent
    /api/plan     -> Planning agent
    /api/run      -> Generator + Reviewer + Executor (background)
    /api/run/{id} -> live status from Execution agent
    /api/history, /api/projects, /api/query -> RCA / ledger
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import threading
import traceback
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import db
from .config import BASE_DIR, SAVED_DIR, STATIC_DIR, UPLOADS_DIR, project_run_dir, slugify
from .agents import discovery as discovery_agent
from .agents import planner as planner_agent
from .agents import generator as generator_agent
from .agents import reviewer as reviewer_agent
from .agents import executor as executor_agent
from .agents import recording as recording_agent

app = FastAPI(title="APEA — Autonomous Performance Engineering Agent")
db.init_db()


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Surface the real error (and log a full traceback) instead of a bare 500."""
    tb = traceback.format_exc()
    try:
        with open(BASE_DIR / "apea_error.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n--- {request.method} {request.url.path} ---\n{tb}\n")
    except Exception:
        pass
    print(tb)
    return JSONResponse(status_code=500,
                        content={"detail": f"{type(exc).__name__}: {exc}"})

# in-memory discovery cache: normalized url -> discovery dict
_DISCOVERY: dict[str, dict] = {}


def _critical_paths(platform: str = "") -> dict:
    """Business-critical call fragments for a platform, from the KB.

    Two kinds of recorded call are not optional load: the storefront cart-add
    (which runs the store's OWN pricing -- skip it and an API add can land the
    line at 0, so the order captures shipping and tax only) and the storefront
    login (skip it and every storefront call runs as a guest). Both are usually
    plain form POSTs, so any bulk "API only" selection drops them, and the run
    still reports 200 everywhere.

    The fragments live in knowledge/rules/platform_rules.yaml, so onboarding
    another storefront is a KB edit, not a code change. The generic block is
    merged in as a floor: a false positive costs one extra call in the script, a
    false negative costs a whole run of zero-value orders that reports success.
    """
    from .knowledge import KB
    out = {"cart_add": [], "login": []}
    for src in ("generic", platform):
        if not src:
            continue
        blk = (KB.platform_rules(src) or {}).get("business_critical_paths") or {}
        for kind in out:
            out[kind].extend(str(x).lower() for x in (blk.get(kind) or []))
    return {k: sorted(set(v)) for k, v in out.items()}


def _critical_kind(path: str, platform: str = "") -> str:
    """'cart_add', 'login' or '' — why this call must not be dropped."""
    low = str(path or "").lower()
    # A framework static mount can contain the same words: Magento's Knockout
    # templates include .../template/cart/add.html. That is a file served by
    # nginx, not the cart controller. The noise filter already drops these, but
    # matching on substrings is broad by design, so exclude them here too.
    from .agents import filter as _flt
    if _flt.is_static_mount_file(low):
        return ""
    for kind, frags in _critical_paths(platform).items():
        if any(f in low for f in frags):
            return kind
    return ""


def _call_sig(step: dict) -> str:
    """Stable identity for a recorded call: 'METHOD /path?query'. Used to match a
    UI selection back to the flow (survives fuse reorder/enrich, which keep
    method+path intact)."""
    return (str(step.get("method") or "GET").upper() + " "
            + str(step.get("path") or "/"))


def _detect_gateway_safe(flow: list) -> Optional[str]:
    """Gateway key Claude/heuristics detected in the recording (for the UI's
    dynamic payment-gateway picker). None if none/unknown. Never raises."""
    try:
        from .agents import browser_journey
        return browser_journey.detect_gateway({"flow": flow or []})
    except Exception:
        return None


def _payment_hints(flow) -> dict:
    """Payment methods this recording actually used, plus the additional_data
    template for each card gateway.

    Both come from data, not from code: the methods from the recording (no
    credentials, no network), the token key from
    knowledge/rules/browser_patterns.yaml. The UI fills the template in when a
    card gateway is picked, so nobody has to remember that CyberSource wants
    `card_id` while Braintree wants `paymentMethodNonce`.
    """
    from .knowledge import KB
    out = {"methods": [], "templates": {}}
    try:
        from .agents import payment as _pay
        out["methods"] = _pay.methods_from_recording(flow or []) or []
    except Exception:
        pass
    try:
        prof = ((KB._section("browser_patterns") or {})
                .get("replay_profiles") or {}).get("vaulted_card_token") or {}
        by_method = prof.get("token_field_by_method") or {}
        extras = prof.get("template_extras") or {}
    except Exception:
        by_method, extras = {}, {}
    # Ranked alternatives to a card, so a suggestion is never "free" (which only
    # applies to zero-total orders) when a real one exists.
    try:
        out["offline_preference"] = [
            str(x).lower() for x in
            ((KB.platform_rules("magento") or {}).get("offline_payments_realistic") or [])]
    except Exception:
        out["offline_preference"] = []
    default = by_method.get("_default", "public_hash")
    # Longest match first so a store-specific code resolves to the right gateway.
    keys = sorted((k for k in by_method if k != "_default"), key=len, reverse=True)
    for m in out["methods"]:
        code = str(m.get("code") or "").lower()
        if m.get("kind") != "hosted" or not code:
            continue
        field = next((by_method[k] for k in keys if k in code), default)
        tpl = {field: "{{payment_token}}"}
        tpl.update(extras)
        out["templates"][m["code"]] = json.dumps(tpl)
    return out


def _platform_of(rec: dict, flow) -> str:
    """Best-matching commerce platform for this recording, or 'generic'."""
    try:
        from .platforms import detect as _d
        return _d(rec if isinstance(rec, dict) else {}, "", flow) or "generic"
    except Exception:
        return "generic"


def _build_calls(rec: dict, platform: str = "") -> list:
    """Classified, de-duplicated list of the REAL calls kept from a recording, for
    the UI's call selector. Every call is `selected: True` by default (APEA has
    already dropped static/noise — these are the actual API/REST/app calls)."""
    from .agents import filter as _flt
    items = rec.get("flow") or rec.get("endpoints") or []
    out, seen = [], set()
    for s in items:
        sig = _call_sig(s)
        if sig in seen:
            continue
        seen.add(sig)
        method = str(s.get("method") or "GET").upper()
        path = str(s.get("path") or "/")
        out.append({
            "sig": sig, "method": method, "path": path,
            "group": s.get("group") or "",
            "label": s.get("label") or s.get("name") or path,
            "kind": _flt.classify_call(method, path, s.get("xhr", False),
                                       s.get("json", False)),
            # Why this call must not be dropped ('' = ordinary load). Computed
            # HERE, from the KB, so the browser does not carry a second copy of
            # the rule that can drift out of step with the server's.
            "critical": _critical_kind(path, platform),
            "selected": True,
        })
    return out


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #
class DiscoverReq(BaseModel):
    url: str
    username: Optional[str] = None
    password: Optional[str] = None
    max_pages: int = 12
    # None = auto (only engages the Playwright re-crawl if the static crawl
    # looks JS-heavy and sparse, and Playwright is installed); True/False
    # force it on/off. Omitting this field preserves the exact prior behavior.
    render_js: Optional[bool] = None


class PlanReq(BaseModel):
    test_type: str = "load"
    expected_users: int = 100
    peak_users: Optional[int] = None
    users: Optional[int] = None
    duration_s: Optional[int] = None
    spawn_rate: Optional[float] = None
    url: Optional[str] = None


class RunReq(BaseModel):
    project_name: str
    url: str
    client: Optional[str] = ""
    test_type: str = "load"
    expected_users: int = 100
    peak_users: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    users: Optional[int] = None
    duration_s: Optional[int] = None
    spawn_rate: Optional[float] = None
    workers: Optional[int] = 1
    users_csv: Optional[str] = None      # server path from /api/upload (kind=users)
    data_csv: Optional[str] = None       # server path from /api/upload (kind=data)
    recording: Optional[str] = None      # server path from /api/upload (kind=recording)
    cart_qty: Optional[int] = 1          # units added per add-to-cart (raise product load)
    captcha_token: Optional[str] = None  # bypass / reCAPTCHA test-key response
    captcha_field: Optional[str] = None  # extra field name to carry the token
    ai_repair: Optional[bool] = False    # AI pre-flight self-repair (needs API key)
    payment_method: Optional[str] = None       # gateway test-mode method code
    payment_additional_data: Optional[str] = None  # JSON: stored-card/test params
    faithful: Optional[bool] = False     # JMeter-style verbatim replay
    strict: Optional[bool] = False       # reproducible mode: no self-heal, no payment auto-switch, fail loud (comparable measurements)
    data_sharing: Optional[str] = "all_threads"  # CSV->thread sharing: "all_threads" (shared, recycle) | "unique" (distinct per user)
    payment_api_replay: Optional[bool] = False   # opt-in: generate API-replay payment (mint sandbox token per VU + correlate into order)
    testplan: Optional[str] = None               # Performance Test Plan path (from /api/upload kind=testplan) -> drives SLA/think/load via execution-plan
    think_min: Optional[float] = None            # think-time min (s) — explicit override (usually prefilled from the test plan)
    think_max: Optional[float] = None            # think-time max (s)
    sla_p95_ms: Optional[float] = None           # SLA: max p95 latency (ms) — explicit override
    sla_error_pct: Optional[float] = None        # SLA: max error rate (%)
    abort_after: Optional[int] = 3       # stop whole run after N checkout fails / 0 = run full duration
    group_throughput: Optional[dict] = None   # {group name: percent executions} per API group
    include_static: Optional[bool] = False   # keep static assets/pages (default: exclude)
    selected_calls: Optional[list] = None    # ["METHOD /path", ...] to keep; None = keep all
    browser_vus: Optional[int] = 0           # >0 enables the real-browser (Playwright) track
    browser_payment: Optional[dict] = None   # e.g. {"gateway":"stripe","submit_selector":"#pay","success_url_contains":"/success"}


class QueryReq(BaseModel):
    text: str


class SaveReq(BaseModel):
    name: str
    run_id: Optional[str] = None          # save the exact script a run used
    # ...or regenerate from cached discovery (save-after-discover):
    project_name: Optional[str] = None
    url: Optional[str] = None
    test_type: str = "load"
    expected_users: int = 100
    peak_users: Optional[int] = None
    users: Optional[int] = None
    duration_s: Optional[int] = None
    spawn_rate: Optional[float] = None
    workers: Optional[int] = 1
    username: Optional[str] = None
    password: Optional[str] = None
    users_csv: Optional[str] = None
    data_csv: Optional[str] = None
    recording: Optional[str] = None
    cart_qty: Optional[int] = 1


class RunSavedReq(BaseModel):
    name: str
    test_type: Optional[str] = None
    expected_users: Optional[int] = None
    peak_users: Optional[int] = None
    users: Optional[int] = None
    duration_s: Optional[int] = None
    spawn_rate: Optional[float] = None
    workers: Optional[int] = 1
    cart_qty: Optional[int] = 1
    captcha_token: Optional[str] = None
    captcha_field: Optional[str] = None
    payment_method: Optional[str] = None
    payment_additional_data: Optional[str] = None
    faithful: Optional[bool] = False


class RenameReq(BaseModel):
    name: str
    new_name: str


class NameReq(BaseModel):
    name: str


class ProjectRenameReq(BaseModel):
    project_id: int
    new_name: str


class RunEditReq(BaseModel):
    run_id: str
    label: Optional[str] = None


# --------------------------------------------------------------------------- #
# uploaded-file helpers (flexible column detection)
# --------------------------------------------------------------------------- #
def _read_csv_rows(path: str):
    rows = []
    with open(path, newline="", encoding="utf-8-sig", errors="ignore") as fh:
        reader = csv.DictReader(fh)
        headers = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
        for r in reader:
            rows.append({(k or "").strip().lower(): (v or "").strip()
                         for k, v in r.items()})
    return headers, rows


def _account_summary(headers, rows) -> dict:
    """What the data file can actually support, as opposed to how many rows it has.

    A platform cart belongs to the CUSTOMER, so two virtual users signed in as
    the same account contend for one basket. On Magento that surfaces as
    "The quote can't be created." -- a functional failure that pollutes the
    measurement rather than an interesting result. So the safe number of
    concurrent users is the number of DISTINCT logins, not the row count.

    Also reports which accounts can pay by card, because a token belongs to one
    customer and a blank one is a run that cannot honour what it declared.
    """
    logins, per_login_token = [], {}
    for r in rows:
        u = (r.get("username") or r.get("email") or "").strip().lower()
        if not u:
            continue
        if u not in per_login_token:
            logins.append(u)
            per_login_token[u] = False
        if (r.get("payment_token") or "").strip():
            per_login_token[u] = True
    products = sorted({(r.get("product_id") or "").strip()
                       for r in rows if (r.get("product_id") or "").strip()})
    without = [u for u in logins if not per_login_token[u]]
    # Which logins DECLARED a card. "No token" is only a problem for these --
    # an invoice payer has no token and needs none, and counting it as missing
    # turns a correct file into an alarming one.
    _card = ("cybersource", "paradoxlabs", "stripe", "braintree", "adyen",
             "authorizenet", "authorize_net", "payflow", "worldpay", "sagepay")
    per_login_card, per_login_method = {}, {}
    for r in rows:
        u = (r.get("username") or r.get("email") or "").strip().lower()
        m = (r.get("payment_method") or "").strip().lower()
        if not u or not m:
            continue
        per_login_method.setdefault(u, m)
        if any(g in m for g in _card) or "{{" in m:
            per_login_card[u] = True
    card_logins = [u for u in logins if per_login_card.get(u)]
    # Every distinct method the FILE asks for. When the UI forces one method
    # instead, these are the rows it silently overrides -- which is invisible
    # otherwise, and reads as the file being wrong.
    declared_methods = sorted({(r.get("payment_method") or "").strip()
                               for r in rows if (r.get("payment_method") or "").strip()})
    offline_logins = [u for u in logins
                      if u not in set(card_logins) and per_login_method.get(u)]
    return {
        "rows": len(rows),
        "logins": logins,
        "unique_logins": len(logins),
        "safe_concurrent_users": len(logins),
        "products": len(products),
        "has_token_column": "payment_token" in (headers or []),
        "with_token": [u for u in logins if per_login_token[u]],
        "without_token": without,
        "card_logins": card_logins,
        "card_without_token": [u for u in card_logins if not per_login_token[u]],
        "declared_methods": declared_methods,
        "offline_logins": offline_logins,
        "method_by_login": per_login_method,
    }


def _pick(row: dict, keys) -> str:
    for k in keys:
        if row.get(k):
            return row[k]
    return ""


def _extract_users(rows) -> list:
    out = []
    for r in rows:
        u = _pick(r, ["username", "user", "email", "userid", "user_id", "login"])
        p = _pick(r, ["password", "pass", "pwd", "passwd"])
        if u or p:
            out.append({"username": u, "password": p})
    return out


def _extract_data(rows) -> list:
    out = []
    for r in rows:
        pid = _pick(r, ["product_id", "productid", "product", "sku", "pid", "item_id", "itemid"])
        kw = _pick(r, ["search_keyword", "keyword", "search", "query", "term", "q",
                       "searchterm", "ndc", "name", "product_name", "title"])
        # any other single/unrecognized column: use its first non-empty value as the search term
        if not pid and not kw:
            for v in r.values():
                if v:
                    kw = v
                    break
        if pid or kw:
            out.append({"product_id": pid, "search_keyword": kw})
    return out


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index():
    idx = STATIC_DIR / "index.html"
    return HTMLResponse(idx.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/health")
def health():
    return {"status": "ok", "locust": executor_agent.locust_available()}


@app.post("/api/restart")
def restart():
    """Kill any running tests and restart the APEA server process in place.

    Re-executes the same command, so it also picks up code changes — no need to
    close and reopen the terminal. The port is reused, so the URL stays the same.
    """
    import os
    import sys
    import time as _time

    def _do():
        _time.sleep(0.6)                       # let the HTTP response flush
        try:
            executor_agent.kill_all()          # don't orphan Locust subprocesses
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            # fallback: spawn a fresh process, then exit this one
            try:
                import subprocess
                subprocess.Popen([sys.executable] + sys.argv, cwd=str(BASE_DIR))
            finally:
                os._exit(0)

    threading.Thread(target=_do, daemon=True).start()
    return {"restarting": True}


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #
@app.post("/api/discover")
def discover(req: DiscoverReq):
    creds = {"username": req.username, "password": req.password} if req.username else None
    result = discovery_agent.crawl(req.url, max_pages=req.max_pages, credentials=creds,
                                   render_js=req.render_js)
    _DISCOVERY[result["base_url"]] = result
    return {
        "base_url": result["base_url"], "reachable": result["reachable"],
        "status_code": result["status_code"], "title": result["title"],
        "domain": result["domain"], "tech": result["tech"],
        "cdn_waf": result["cdn_waf"],
        "pages": result["pages"], "forms_count": len(result["forms"]),
        "apis_count": len(result["apis"]), "journeys": result["journeys"],
        "search_endpoint": result["search_endpoint"],
        "login_detected": result["login_form"] is not None,
        "notes": result["notes"],
    }


@app.post("/api/plan")
def make_plan(req: PlanReq):
    overrides = {}
    if req.users:
        overrides["users"] = req.users
    if req.duration_s:
        overrides["duration_s"] = req.duration_s
    if req.spawn_rate:
        overrides["spawn_rate"] = req.spawn_rate
    cfg = planner_agent.plan(req.test_type, req.expected_users, req.peak_users,
                             overrides=overrides)
    return cfg


@app.get("/api/test-types")
def test_types():
    return [{"key": k, "label": v["label"], "purpose": v["purpose"]}
            for k, v in planner_agent.TEST_TYPES.items()]


@app.post("/api/plan/ai-suggest")
def plan_ai_suggest(req: PlanReq):
    """AI-tuned plan: rationale + suggested numeric overrides (+ a recommended
    test suite for the 'test_plan' type). Falls back to empty without an API key."""
    base = discovery_agent._norm_base(req.url) if getattr(req, "url", None) else None
    disc = _DISCOVERY.get(base) if base else None
    disc = disc or {"journeys": [], "domain": None, "tech": []}
    overrides = {}
    if req.users:
        overrides["users"] = req.users
    if req.duration_s:
        overrides["duration_s"] = req.duration_s
    if req.spawn_rate:
        overrides["spawn_rate"] = req.spawn_rate
    current = planner_agent.plan(req.test_type, req.expected_users, req.peak_users,
                                 overrides=overrides)
    result = planner_agent.ai_suggest(disc, req.test_type, req.expected_users,
                                      req.peak_users, current)
    result["ai_enabled"] = _ai_status()["enabled"]
    return result


@app.post("/api/upload")
async def upload(kind: str = Form(...), file: UploadFile = File(...)):
    """Accept a CSV (users/data) or a recording (JMX/HAR/YAML).

    kind = "users" | "data" | "recording". Saves the file and returns a
    server-side path plus a short summary (row/endpoint counts) for the UI.
    """
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe = Path(file.filename or "upload").name
    dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{safe}"
    dest.write_bytes(await file.read())

    summary = {}
    try:
        if kind == "users":
            _, rows = _read_csv_rows(dest)
            items = _extract_users(rows)
            summary = {"rows": len(items),
                       "detected": "username/password" if items else "no user columns found"}
        elif kind == "data":
            _, rows = _read_csv_rows(dest)
            items = _extract_data(rows)
            kws = sum(1 for i in items if i["search_keyword"])
            pids = sum(1 for i in items if i["product_id"])
            summary = {"rows": len(items), "keywords": kws, "product_ids": pids}
        elif kind == "recording":
            rec = recording_agent.parse_recording(dest)
            summary = {"source": rec.get("source"), "endpoints": len(rec.get("endpoints", [])),
                       "base_url": rec.get("base_url"), "error": rec.get("error")}
        elif kind == "testplan":
            # Performance Test Plan document (PDF today). Just save + report size here;
            # /api/analyze-test-plan does the extraction.
            summary = {"bytes": dest.stat().st_size, "format": safe.rsplit(".", 1)[-1].lower()}
        else:
            raise HTTPException(status_code=400, detail=f"Unknown upload kind: {kind}")
    except HTTPException:
        raise
    except Exception as exc:
        summary = {"error": f"{type(exc).__name__}: {exc}"}

    return {"path": str(dest), "kind": kind, "filename": safe, "summary": summary}


class AnalyzeReq(BaseModel):
    recording: str
    include_static: Optional[bool] = False   # keep static assets/pages (default: exclude)


class TestPlanReq(BaseModel):
    testplan: str                            # server-side path from /api/upload (kind=testplan)
    text: Optional[str] = None               # OR pasted plan text (no PDF parser needed)


@app.post("/api/analyze-test-plan")
def analyze_test_plan(req: TestPlanReq):
    """Analyze a Performance Test Plan (PDF or pasted text) into the test-plan.json
    contract: objectives, SLA, load profile, and the AVAILABLE test types the QA
    engineer can choose from. Does NOT auto-select or run any test type."""
    from .agents import test_plan_analyzer
    try:
        plan = test_plan_analyzer.analyze(
            pdf_path=(req.testplan if req.testplan and not req.text else None),
            text=req.text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")
    # persist alongside the upload for the pipeline to consume later
    try:
        if req.testplan:
            (Path(req.testplan).with_suffix(".test-plan.json")).write_text(
                json.dumps(plan, indent=2), encoding="utf-8")
    except Exception:
        pass
    return plan


class AppKnowledgeReq(BaseModel):
    url: Optional[str] = None                 # live-crawl a target (needs the store reachable)
    recording: Optional[str] = None           # OR assemble from an uploaded recording
    max_pages: Optional[int] = 40             # crawl depth — analyze more of the whole site


@app.post("/api/application-knowledge")
def application_knowledge_ep(req: AppKnowledgeReq):
    """Build/refresh the Application Knowledge Base for a target — independently of a
    specific run. Assembles from an uploaded recording, or from a live crawl of `url`.
    Degrades to a minimal KB rather than erroring when the store isn't reachable."""
    from .agents import application_knowledge
    disc, base = {}, (req.url or "")
    try:
        if req.recording:
            disc = recording_agent.parse_recording(req.recording) or {}
            base = req.url or disc.get("base_url", "")
        elif req.url:
            disc = discovery_agent.crawl(req.url, max_pages=req.max_pages or 40) or {}  # live crawl — needs the store
            base = req.url
        else:
            raise HTTPException(status_code=400,
                                detail="Provide a recording path or a target url.")
    except HTTPException:
        raise
    except Exception as exc:
        disc = {"base_url": base, "error": f"{type(exc).__name__}: {exc}"}
    return application_knowledge.build(disc, target_url=base, source="refresh")


class DataPoolsReq(BaseModel):
    url: Optional[str] = None                  # live-crawl a target (needs the store reachable)
    recording: Optional[str] = None            # OR bind pools to an uploaded recording's transactions
    max_pages: Optional[int] = 40              # crawl depth
    live_products: Optional[bool] = False      # opt-in: fill the PDP pool from storefront GraphQL (read-only)
    uploads: Optional[dict] = None             # {"pdp.csv": "<server path>", ...} named CSVs that OVERRIDE detection


@app.post("/api/detect-data-pools")
def detect_data_pools_ep(req: DataPoolsReq):
    """Detect per-transaction data pools (PLP / PDP / manufacturer / nav / search) from
    the Playwright crawl + storefront GraphQL, bind each to the recording's transaction
    groups, and let uploaded CSVs override detection. Returns the data-pools.json
    manifest. Additive — the default unified-CSV run path is unaffected."""
    from .agents import data_pools
    disc, flow_steps, base = {}, [], (req.url or "")
    try:
        if req.recording:
            disc = recording_agent.parse_recording(req.recording) or {}
            flow_steps = disc.get("flow") or []
            base = req.url or disc.get("base_url", "")
        if req.url:
            try:
                crawl = discovery_agent.crawl(req.url, max_pages=req.max_pages or 40) or {}
                for _k in ("pages", "search_endpoint", "base_url", "apis"):
                    if crawl.get(_k):
                        disc[_k] = crawl[_k]
                base = req.url
            except Exception:
                pass
    except Exception as exc:
        return {"target": base, "pools": {}, "bindings": [], "coverage": {},
                "notes": ["detect error: %s: %s" % (type(exc).__name__, exc)]}
    live = None
    if req.live_products and base:
        try:
            from .agents import live_store
            store = live_store.store_from_paths([s.get("path") for s in (flow_steps or [])])
            live = live_store.discover_products(base, count=200, store=store)
        except Exception:
            live = None
    return data_pools.detect(disc, flow_steps=flow_steps, live_products=live,
                             uploads=req.uploads, target_url=base)


class GenDataReq(BaseModel):
    recording: Optional[str] = None           # recording -> discovery -> application knowledge
    testplan: Optional[str] = None            # test plan -> sizing + throughput
    test_type: Optional[str] = "load"         # selected test type -> execution plan
    users: Optional[int] = None               # authoritative row count (the Users field the QA engineer sees)
    data_csv: Optional[str] = None            # existing rows to seed realistic values (creds/card/address)
    customer_mode: Optional[str] = "existing"  # existing | synthetic
    live_products: Optional[bool] = False      # opt-in: find real in-stock, priced products (read-only)
    register_customers: Optional[bool] = False  # opt-in: create real accounts on the store (WRITE — test/staging only)


@app.post("/api/generate-test-data")
def generate_test_data_ep(req: GenDataReq):
    """Intelligent Test Data Generator — realistic, executable test data sized for the
    selected test, from the Application Knowledge Base + Test Plan + recording. Returns
    the generated CSV contents + a validation gate + a suggested run config (throughput,
    test type, users). The generated testdata.csv path becomes the run's data CSV."""
    from .agents import (application_knowledge, performance_planner,
                         test_plan_analyzer, test_data_generator)
    disc = {}
    try:
        if req.recording:
            disc = recording_agent.parse_recording(req.recording) or {}
    except Exception:
        disc = {}
    akb = application_knowledge.build(disc, target_url=disc.get("base_url", ""))
    test_plan = {}
    if req.testplan:
        try:
            _tpj = Path(req.testplan).with_suffix(".test-plan.json")
            test_plan = (json.loads(_tpj.read_text(encoding="utf-8")) if _tpj.exists()
                         else test_plan_analyzer.analyze(pdf_path=str(req.testplan)))
        except Exception:
            test_plan = {}
    ep = performance_planner.build(test_plan, req.test_type or "load")
    if req.users:                             # the Users field the user sees wins over the plan's (often noisy) extracted value
        ep["users"] = int(req.users)
    existing = _read_csv_rows(req.data_csv)[1] if req.data_csv else []
    _bflow = {"name": "recorded flow",
              "steps": [(s.get("label") or s.get("name") or s.get("path"))
                        for s in (disc.get("flow") or [])][:30]}
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = UPLOADS_DIR / ("gendata_" + uuid.uuid4().hex[:8])
    _live = bool(req.live_products or req.register_customers)
    _store = ""
    try:
        from .agents import live_store
        _store = live_store.store_from_paths(
            [s.get("path") or s.get("url") for s in (disc.get("flow") or [])])
    except Exception:
        _store = ""
    result = test_data_generator.generate(
        akb, test_plan=test_plan, execution_plan=ep, business_flow=_bflow,
        existing_rows=existing, out_dir=out_dir,
        options={"customer_mode": req.customer_mode or "existing",
                 "live_base_url": (disc.get("base_url", "") if _live else None),
                 "store": _store,
                 "live_products": bool(req.live_products),
                 "register_customers": bool(req.register_customers)})
    # read back file contents so the UI can offer downloads (small files)
    contents = {}
    for _name, _path in (result.get("files") or {}).items():
        try:
            contents[_name] = Path(_path).read_text(encoding="utf-8")
        except Exception:
            pass
    result["csv_files"] = contents
    result["testdata_path"] = (result.get("files") or {}).get("testdata")
    return result


class ValidateReq(BaseModel):
    data_csv: str
    recording: Optional[str] = None


class CrawlRecordReq(BaseModel):
    """Standalone alternative to uploading a recording: crawl `url` live with
    Playwright (search / login / add-to-cart / cart view, best-effort), and —
    if `recording` is ALSO given — merge the two into one flow, so the
    checkbox in the recording-upload UI can use Playwright to fill gaps in an
    uploaded file instead of replacing it."""
    url: str
    username: Optional[str] = None
    password: Optional[str] = None
    recording: Optional[str] = None
    max_pages: int = 12
    search_term: Optional[str] = None
    include_static: Optional[bool] = False   # keep static assets/pages (default: exclude)


@app.post("/api/analyze-recording")
def analyze_recording(req: AnalyzeReq):
    """From an uploaded recording: derive the journey, the dynamic values APEA will
    CORRELATE, and the input fields to PARAMETERIZE — plus a ready sample CSV."""
    from .agents import parameterization
    rec = recording_agent.parse_recording(req.recording, include_static=bool(req.include_static))
    flow = rec.get("flow") or []
    ui_steps = rec.get("ui_steps") or []
    an = parameterization.analyze(flow, rec.get("selenium_inputs"), ui_steps)
    jname, jsteps = (recording_agent._derive_journey(flow) if flow
                     else ("Recorded Journey", []))
    return {
        "source": rec.get("source"), "base_url": rec.get("base_url"),
        "steps": len(flow), "error": rec.get("error"),
        "journey": {"name": jname, "steps": jsteps},
        "correlations": an["correlations"], "groups": an["groups"],
        "columns": an["columns"], "uploads_needed": an["uploads_needed"],
        "required_columns": an.get("required_columns", []),
        "product_columns": an.get("product_columns", []),
        "optional_columns": an.get("optional_columns", []),
        "api_groups": recording_agent.api_call_groups(flow),
        "static_dropped": rec.get("static_dropped", 0),
        # the real calls kept for the script (selectable in the UI) + the noise
        # APEA filtered out (shown read-only so the filtering is transparent).
        "calls": _build_calls(rec, _platform_of(rec, flow)),
        "payment": _payment_hints(flow),
        "dropped": rec.get("dropped", []),
        "detected_gateway": _detect_gateway_safe(flow),
        "sample_csv": an["sample_csv"], "notes": an["notes"],
        # every recorded browser action (clicks, waits, typed fields — not just
        # the 3 categories folded into parameterization), for the report only;
        # never used to drive the replayed flow.
        "ui_steps": ui_steps[:300],
        "ai_analysis": rec.get("ai_analysis") or {},   # LLM journey / warnings / platform
        "ai_enabled": _ai_status()["enabled"],
    }


@app.post("/api/crawl-record")
def crawl_record(req: CrawlRecordReq):
    """Crawl+record `req.url` with Playwright (search/login/add-to-cart/cart —
    see discovery_agent.crawl_and_record). If `req.recording` is also given,
    the two are merged (recording.merge_into_discovery) rather than one
    replacing the other. Returns the SAME shape as /api/analyze-recording so
    the frontend can render it with the exact same code."""
    from .agents import parameterization
    creds = {"username": req.username, "password": req.password} if req.username else None
    has_recording = bool(req.recording)
    rec = (recording_agent.parse_recording(req.recording, include_static=bool(req.include_static))
           if has_recording else {})
    base_url = rec.get("base_url") or req.url
    if not base_url:
        raise HTTPException(status_code=400, detail="Provide a URL to crawl.")

    crawled = discovery_agent.crawl_and_record(
        base_url, max_pages=req.max_pages, credentials=creds,
        search_term=(req.search_term or "test"))
    if crawled is None and not has_recording:
        raise HTTPException(
            status_code=400,
            detail="Playwright isn't installed (`pip install playwright && "
                   "playwright install chromium`), and no recording was uploaded "
                   "to fall back on.")

    base = discovery_agent._norm_base(base_url)
    disc = crawled or {"base_url": base, "domain": "Recorded", "tech": [], "pages": [],
                       "forms": [], "apis": [], "journeys": [], "reachable": True,
                       "status_code": 200, "notes": []}
    crawled_flow = list(crawled.get("flow") or []) if crawled else []
    if has_recording:
        # merge_into_discovery REPLACES discovery["flow"] wholesale with the
        # recording's flow — fine when there's no crawl, but it would silently
        # discard everything Playwright just captured. Re-append anything
        # Playwright saw that the recording didn't, so "enhance with
        # Playwright" actually fills gaps instead of being overwritten by it.
        recording_agent.merge_into_discovery(disc, rec)
        if crawled_flow:
            rec_flow = disc.get("flow") or []
            seen = {(s.get("method"), s.get("path")) for s in rec_flow}
            for s in crawled_flow:
                key = (s.get("method"), s.get("path"))
                if key not in seen:
                    rec_flow.append(s)
                    seen.add(key)
            disc["flow"] = rec_flow
    _DISCOVERY[base] = disc

    flow = disc.get("flow") or []
    ui_steps = rec.get("ui_steps") or []
    an = parameterization.analyze(flow, rec.get("selenium_inputs"), ui_steps)
    jname, jsteps = (recording_agent._derive_journey(flow) if flow
                     else ("Recorded Journey", []))

    if crawled and has_recording:
        source = "recording+playwright"
    elif crawled:
        source = "playwright"
    else:
        source = rec.get("source") or "recording"

    note_parts = []
    if crawled and crawled.get("notes"):
        note_parts.append("Playwright:\n" + "\n".join(f"- {n}" for n in crawled["notes"]))
    if an.get("notes"):
        note_parts.append(an["notes"])
    combined_notes = "\n\n".join(note_parts) or None

    return {
        "source": source, "base_url": base,
        "steps": len(flow), "error": rec.get("error"),
        "journey": {"name": jname, "steps": jsteps},
        "correlations": an["correlations"], "groups": an["groups"],
        "columns": an["columns"], "uploads_needed": an["uploads_needed"],
        "required_columns": an.get("required_columns", []),
        "product_columns": an.get("product_columns", []),
        "optional_columns": an.get("optional_columns", []),
        "api_groups": recording_agent.api_call_groups(flow),
        "static_dropped": rec.get("static_dropped", 0),
        # calls come from the MERGED flow (recording + anything Playwright added);
        # dropped noise is from the recording parse.
        "calls": _build_calls({"flow": flow}, _platform_of(rec, flow)),
        "payment": _payment_hints(flow),
        "dropped": rec.get("dropped", []),
        "detected_gateway": _detect_gateway_safe(flow),
        "sample_csv": an["sample_csv"], "notes": combined_notes,
        "ui_steps": ui_steps[:300],
        "ai_analysis": rec.get("ai_analysis") or {},
        "ai_enabled": _ai_status()["enabled"],
        "playwright_used": bool(crawled),
    }


@app.post("/api/validate-data")
def validate_data(req: ValidateReq):
    """Validate an uploaded data CSV against the columns the recording needs."""
    from .agents import parameterization
    required = []
    if req.recording:
        rec = recording_agent.parse_recording(req.recording)
        required = parameterization.analyze(
            rec.get("flow") or [], rec.get("selenium_inputs"),
            rec.get("ui_steps")).get("columns", [])
    headers, rows = _read_csv_rows(req.data_csv)
    result = parameterization.validate(required, rows)
    # What the file can actually SUPPORT, for the readiness panel: distinct
    # logins bound safe concurrency, and a card needs a token per account.
    try:
        result["accounts"] = _account_summary(headers, rows)
    except Exception:
        pass
    return result


# --- card enrolment, run from the UI ---------------------------------------
# Capturing a card token used to mean leaving APEA, running a script in a
# terminal, and coming back. The values it produces are the ones THIS page is
# already validating, so the round trip was pure friction -- and the step most
# likely to be skipped is the one that makes a card run possible at all.
_ENROL_JOBS: dict = {}


class EnrolReq(BaseModel):
    """Fetch a card token for every account in the uploaded CSV that needs one."""
    data_csv: str                              # server path from /api/upload
    base_url: str                              # store root, including any store path
    company: Optional[str] = "APEA Load Test"
    card: Optional[str] = "success"
    cvv: Optional[str] = None
    headed: bool = True                        # watching it is the point, on a first run


@app.post("/api/enrol-cards")
def enrol_cards(req: EnrolReq):
    """Start enrolment in the background and return a job id to poll.

    It runs as a subprocess rather than in-process on purpose: it drives a real
    browser for minutes at a time, and the API must stay responsive. The script
    edits the CSV in place, so when the job finishes the page just re-validates
    the file it already has.
    """
    import subprocess
    import threading

    csv_path = Path(req.data_csv)
    if not csv_path.exists():
        return {"error": "data CSV not found -- upload it first"}
    script = Path(__file__).resolve().parent.parent / "enrol_cards.py"
    if not script.exists():
        return {"error": "enrol_cards.py is not present in this checkout"}

    cmd = [sys.executable, str(script),
           "--csv", str(csv_path),
           "--base-url", req.base_url.rstrip("/"),
           "--card", req.card or "success"]
    if req.company:
        cmd += ["--company", req.company]
    if req.cvv:
        cmd += ["--cvv", req.cvv]
    if req.headed:
        cmd += ["--headed"]

    job = uuid.uuid4().hex[:8]
    _ENROL_JOBS[job] = {"lines": [], "done": False, "rc": None,
                        "csv": str(csv_path)}

    def _run():
        rec = _ENROL_JOBS[job]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1, cwd=str(script.parent))
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    # Belt and braces: this output reaches a browser, and a card
                    # number must not ride along even if the script changes.
                    rec["lines"].append(re.sub(r"\b\d(?:[ -]?\d){12,18}\b",
                                               "[card redacted]", line))
                    del rec["lines"][:-400]
            proc.wait()
            rec["rc"] = proc.returncode
        except Exception as exc:
            rec["lines"].append("enrolment could not start: %s" % exc)
            rec["rc"] = -1
        finally:
            rec["done"] = True

    threading.Thread(target=_run, daemon=True).start()
    return {"job": job, "command": " ".join(cmd[1:])}


@app.get("/api/enrol-cards/csv")
def enrol_cards_csv(job: str):
    """Hand back the data file enrolment just wrote into.

    Enrolment edits APEA's uploaded COPY, and the file the operator uploaded
    from stays as it was. That drift has now cost two runs: a page showing
    tokens, a file on disk without them, and warnings that were correct about a
    copy nobody was looking at. Offering the updated file back closes it.

    The path comes from this job's own record, never from the query string, so
    there is nothing here to point at another file.
    """
    from fastapi.responses import FileResponse

    rec = _ENROL_JOBS.get(job)
    if rec is None:
        return {"error": "unknown job"}
    src = Path(rec.get("csv") or "")
    if not src.exists():
        return {"error": "that data file is no longer on disk"}
    # Strip the upload's random prefix so the download keeps the operator's own
    # filename -- saving it should overwrite the file they started with.
    name = src.name.split("_", 1)[-1] if "_" in src.name else src.name
    return FileResponse(str(src), media_type="text/csv", filename=name)


@app.get("/api/enrol-cards/status")
def enrol_cards_status(job: str, since: int = 0):
    """Poll a running enrolment. Returns only the lines the caller has not seen."""
    rec = _ENROL_JOBS.get(job)
    if rec is None:
        return {"error": "unknown job"}
    lines = rec["lines"]
    return {"lines": lines[since:], "next": len(lines),
            "done": rec["done"], "rc": rec["rc"]}


class PaymentMethodsReq(BaseModel):
    """List the target's real payment methods for the config UI. A live probe
    (url + credentials) is most accurate; a recording lets it fall back to the
    methods actually seen. `sku` is optional — used to seed an empty cart so
    minimum-order-gated methods still appear."""
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    recording: Optional[str] = None
    rest_prefix: Optional[str] = None
    sku: Optional[str] = None


@app.post("/api/payment-methods")
def payment_methods(req: PaymentMethodsReq):
    """Detect the payment methods available on the target and tag each as
    'hosted' (card gateway — needs the browser track) or 'offline' (HTTP-
    replayable), so the user can SELECT one before a script is generated."""
    from .agents import payment as payment_agent

    flow = None
    rest_prefix = req.rest_prefix
    base = req.url
    if req.recording:
        try:
            rec = recording_agent.parse_recording(req.recording)
            flow = rec.get("flow") or []
            if not base:
                base = rec.get("base_url")
            if not rest_prefix:                    # derive store-code REST prefix
                from .agents.generator import _derive_rest_prefix
                for s in flow:
                    if s.get("rest"):
                        rp = _derive_rest_prefix(s.get("path", ""))
                        if rp:
                            rest_prefix = rp
                            break
        except Exception:
            flow = None

    result = payment_agent.list_payment_methods(
        base_url=base, username=req.username, password=req.password,
        rest_prefix=rest_prefix, recording_flow=flow, sku=req.sku)
    # summarise for the UI: how many need the browser track vs run over HTTP
    methods = result.get("methods") or []
    result["hosted_count"] = sum(1 for m in methods if m.get("kind") == "hosted")
    result["offline_count"] = sum(1 for m in methods if m.get("kind") == "offline")
    return result


@app.post("/api/run")
def run(req: RunReq):
    if not executor_agent.locust_available():
        raise HTTPException(status_code=400,
                            detail="Locust is not installed. Run: pip install -r requirements.txt")
    try:
        creds = {"username": req.username, "password": req.password} if req.username else None

        # Recording-first: derive the target + a minimal discovery from the
        # uploaded recording — NO site crawl. Crawl only as a fallback when a URL
        # is given without a recording.
        rec_base = None
        if req.recording:
            rec_base = recording_agent.parse_recording(req.recording).get("base_url")
        base = discovery_agent._norm_base(req.url) if req.url else (
            discovery_agent._norm_base(rec_base) if rec_base else "")
        if not base:
            raise HTTPException(status_code=400,
                                detail="Upload a BlazeMeter recording (or provide a target URL).")
        disc = _DISCOVERY.get(base)
        if disc is None:
            if req.recording:
                disc = {"base_url": rec_base or base, "domain": "Recorded",
                        "tech": [], "pages": [], "forms": [], "apis": [],
                        "journeys": [], "reachable": True, "status_code": 200, "notes": []}
            else:
                disc = discovery_agent.crawl(req.url, credentials=creds)
            _DISCOVERY[disc["base_url"]] = disc

        # parse uploaded CSVs (flexible column detection). The sample CSV combines
        # username/password AND product/search in ONE file, and users often upload
        # it as the "data" CSV — so scan BOTH files for BOTH kinds of columns.
        _users_rows = _read_csv_rows(req.users_csv)[1] if req.users_csv else []
        _data_rows = _read_csv_rows(req.data_csv)[1] if req.data_csv else []
        uploaded_users = _extract_users(_users_rows) or _extract_users(_data_rows)
        uploaded_data = _extract_data(_data_rows) or _extract_data(_users_rows)

        # --- Recorder / Transcription: fuse the recording(s) into one clean flow.
        # (Recording-first — the uploaded BlazeMeter/JMX/HAR/YAML is the source.) ---
        from .agents import transcription
        _sources, rec = [], None
        if disc.get("flow"):                       # already-parsed flow in the cache
            _sources.append({"source": "recording", "flow": disc["flow"]})
        if req.recording:
            rec = recording_agent.parse_recording(req.recording,
                                                  include_static=bool(req.include_static))
            if rec.get("flow"):
                _sources.append({"source": rec.get("source", "recording"),
                                 "flow": rec["flow"]})

        if _sources:
            fused = transcription.fuse(_sources, base_url=disc.get("base_url"))
            recording_agent.merge_into_discovery(disc, fused)
        elif rec:                                   # recording had endpoints but no flow
            recording_agent.merge_into_discovery(disc, rec)
        # transcription.fuse() drops ui_steps — carry the recording's Selenium
        # steps onto discovery so the browser track can extract the REAL payment
        # iframe/field selectors from them.
        if rec and rec.get("ui_steps") and not disc.get("ui_steps"):
            disc["ui_steps"] = rec["ui_steps"]
        # Persist recording-harvested T&C agreement ids onto discovery so a SAVED
        # script keeps them (discovery.json is what saved runs regenerate from).
        if rec and rec.get("agreement_ids") and not disc.get("agreement_ids"):
            disc["agreement_ids"] = rec["agreement_ids"]

        # User curation: keep only the calls the user ticked in the UI selector.
        # None (default) = keep every call, so existing behaviour is unchanged.
        if req.selected_calls is not None:
            _sel = set(req.selected_calls)
            _flow = disc.get("flow") or []
            _kept = [s for s in _flow if _call_sig(s) in _sel]
            if not _kept:
                raise HTTPException(
                    status_code=400,
                    detail="No recorded calls were selected for the script. Select at "
                           "least the REST/API calls that make up the journey.")
            # Warn loudly rather than block: on a store where the API prices
            # correctly, excluding these is a legitimate choice. What is NOT
            # acceptable is dropping them silently, because every metric still
            # reads as a pass while the orders are wrong.
            from .platforms import detect as _detect_platform
            _platform = _detect_platform(disc, req.url or "", _flow)
            _kept_sigs = {_call_sig(s) for s in _kept}
            _dropped_critical = [(_call_sig(s), _critical_kind(s.get("path") or "", _platform))
                                 for s in _flow
                                 if _call_sig(s) not in _kept_sigs
                                 and _critical_kind(s.get("path") or "", _platform)]
            if _dropped_critical:
                _why = {
                    "cart_add": "the item would be added over the API instead, which on a "
                                "store that prices in its own cart controller lands the line "
                                "at 0 -- every order worth shipping and tax only",
                    "login": "the storefront session would never be authenticated, so those "
                             "calls run as a guest and land in a guest cart",
                }
                _warn = ("Business-critical call(s) excluded from the script (%s): %s. "
                         % (_platform,
                            ", ".join("%s [%s]" % (sig, kind)
                                      for sig, kind in _dropped_critical[:3]))
                         + "; ".join(sorted({_why[k] for _s, k in _dropped_critical}))
                         + ". Re-select unless this is deliberate.")
                _dropped_pricing = [sig for sig, _k in _dropped_critical]
                disc.setdefault("selection_warnings", []).append(_warn)
                print("[apea] selection warning: " + _warn)
            disc["flow"] = _kept

        overrides = {}
        if req.users:
            overrides["users"] = req.users
        if req.duration_s:
            overrides["duration_s"] = req.duration_s
        if req.spawn_rate:
            overrides["spawn_rate"] = req.spawn_rate
        # Explicit think-time / SLA overrides (usually prefilled from the test plan
        # in the UI, but the user can edit them — an explicit value wins over the
        # plan's own via the skip flags passed to apply_to_plan_cfg below).
        if req.think_min is not None:
            overrides["think_min"] = req.think_min
        if req.think_max is not None:
            overrides["think_max"] = req.think_max
        if req.sla_p95_ms is not None:
            overrides["p95_ms"] = req.sla_p95_ms
        if req.sla_error_pct is not None:
            overrides["error_threshold"] = req.sla_error_pct
        _explicit_think = ("think_min" in overrides or "think_max" in overrides)
        _explicit_sla = ("p95_ms" in overrides or "error_threshold" in overrides)

        # Phase 2 — Performance Planner: if a Performance Test Plan was uploaded,
        # build the execution-plan contract and let its numbers FILL any workload
        # value the user did not explicitly override (explicit UI override wins).
        _ep = None
        if req.testplan:
            try:
                from .agents import performance_planner, test_plan_analyzer
                _tp_path = Path(req.testplan)
                _tp_json = _tp_path.with_suffix(".test-plan.json")
                _test_plan = (json.loads(_tp_json.read_text(encoding="utf-8"))
                              if _tp_json.exists()
                              else test_plan_analyzer.analyze(pdf_path=str(_tp_path)))
                _bflow = {"name": "recorded flow",
                          "steps": [(s.get("label") or s.get("name") or s.get("path"))
                                    for s in (disc.get("flow") or [])][:20]}
                _ep = performance_planner.build(_test_plan, req.test_type, business_flow=_bflow)
                for _k, _v in performance_planner.to_overrides(_ep).items():
                    overrides.setdefault(_k, _v)   # UI override wins; plan fills the gaps
            except Exception:
                _ep = None

        plan_cfg = planner_agent.plan(req.test_type, req.expected_users, req.peak_users,
                                      overrides=overrides)
        if _ep:                                    # carry the plan's SLA + think time
            performance_planner.apply_to_plan_cfg(plan_cfg, _ep,
                                                  skip_think=_explicit_think, skip_sla=_explicit_sla)
        plan_cfg["workers"] = max(1, int(req.workers or 1))
        plan_cfg["data_sharing"] = (req.data_sharing or "all_threads")  # CSV->thread sharing mode
        plan_cfg["strict"] = bool(req.strict)   # reproducible mode (no self-heal / no payment auto-switch)
        if req.payment_api_replay:           # opt-in API-replay payment codegen
            plan_cfg["payment_api_replay"] = True
        if req.group_throughput:             # per-API-group Percent Executions
            plan_cfg["group_throughput"] = req.group_throughput
        if int(req.browser_vus or 0) > 0:    # opt-in real-browser (Playwright) track
            _bt = {"enabled": True, "vus": int(req.browser_vus)}
            if req.browser_payment:
                _bt["payment"] = req.browser_payment
            plan_cfg["browser_track"] = _bt
        workload = planner_agent.workload_model(disc, plan_cfg)

        run_id = uuid.uuid4().hex[:12]
        run_dir = project_run_dir(req.project_name, disc["base_url"], run_id)

        if _ep:                                    # persist the execution-plan contract
            try:
                (Path(run_dir) / "execution-plan.json").write_text(
                    json.dumps(_ep, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Application Knowledge Base — a per-target model of the whole application
        # (navigation, API surface, business objects, auth), assembled from
        # discovery and refreshable independently. Best-effort; never blocks a run.
        try:
            from .agents import application_knowledge
            _akb = application_knowledge.build(disc, target_url=disc.get("base_url", ""))
            (Path(run_dir) / "application-knowledge.json").write_text(
                json.dumps(_akb, indent=2), encoding="utf-8")
        except Exception:
            pass

        # Multi-pool data manifest — detect per-transaction data pools (PLP / PDP /
        # manufacturer / nav / search) from the crawl and bind them to the recording's
        # transaction groups. Best-effort, purely additive: it is persisted for review
        # and for the generator to consume later; the default unified-CSV path is
        # unaffected whether or not this succeeds.
        try:
            from .agents import data_pools as _dp
            _pools = _dp.detect(disc, flow_steps=(disc.get("flow") or []),
                                live_products=None, target_url=disc.get("base_url", ""))
            (Path(run_dir) / "data-pools.json").write_text(
                json.dumps(_pools, indent=2), encoding="utf-8")
        except Exception:
            pass

        # raw rows (ALL columns — card/shipping/etc.) preserved verbatim for
        # generic parameterization; prefer the data CSV, else the users CSV.
        _raw_rows = (_read_csv_rows(req.data_csv)[1] if req.data_csv
                     else (_read_csv_rows(req.users_csv)[1] if req.users_csv else []))

        # GATE: block generation if an uploaded data CSV has blocking issues
        # (mirrors the UI gate, so a direct API call can't bypass it).
        if req.data_csv:
            from .agents import parameterization as _pz
            _sel = None
            try:
                _sel = recording_agent.parse_recording(req.recording).get("selenium_inputs") \
                    if req.recording else None
            except Exception:
                _sel = None
            _req_cols = _pz.analyze(disc.get("flow") or [], _sel,
                                    disc.get("ui_steps")).get("columns", [])
            _v = _pz.validate(_req_cols, _raw_rows)
            if not _v.get("ok"):
                raise HTTPException(status_code=400,
                                    detail="Data CSV has blocking issues — "
                                    + "; ".join(_v.get("messages", []) or ["invalid data CSV"]))

        # Recording-first credential fallback: if NO credentials CSV was provided,
        # authenticate with the credentials the recording itself captured (Selenium
        # `type` actions) so a self-contained recording runs without a manual CSV.
        # Also seed a search term when the data CSV has none. (Single account —
        # for real multi-user load, upload a credentials CSV.)
        _sel_in = (rec.get("selenium_inputs") if rec else None) or []

        def _sel_val(field):
            return next((s.get("sample") for s in _sel_in
                         if s.get("field") == field and s.get("sample")), "")
        if not creds and not uploaded_users:
            _u = _sel_val("username")
            if _u:
                creds = {"username": _u, "password": _sel_val("password")}
        if not uploaded_data and _sel_val("search_keyword"):
            uploaded_data = [{"search_keyword": _sel_val("search_keyword")}]

        # Hand the resolved credentials to the real-browser track so it can log in
        # and reach checkout (prefer explicit creds, else the first uploaded user).
        if plan_cfg.get("browser_track"):
            _bcred = creds or (uploaded_users[0] if uploaded_users else None)
            if _bcred:
                plan_cfg["browser_track"]["credentials"] = _bcred

        _gen_kwargs = dict(credentials=creds, users=uploaded_users, data=uploaded_data,
                           cart_qty=req.cart_qty or 1, captcha_token=req.captcha_token or "",
                           captcha_field=req.captcha_field or "",
                           payment_method=req.payment_method or "",
                           payment_additional_data=req.payment_additional_data or "",
                           data_rows=_raw_rows, faithful=bool(req.faithful),
                           abort_after=(3 if req.abort_after is None else int(req.abort_after)),
                           agreement_ids=((rec.get("agreement_ids") if rec else None)
                                          or (disc.get("agreement_ids") or [])))
        gen = generator_agent.generate(disc, plan_cfg, run_dir, **_gen_kwargs)

        # Snapshot the generated script into run history (final script + errors +
        # root cause are added at finalize) so 'generated vs final' can be diffed.
        try:
            db.save_run_artifacts(run_id, generated_script=gen.get("script"))
        except Exception:
            pass

        # Optional agentic pre-flight self-repair (off the load hot path).
        repair_info = None
        if req.ai_repair:
            from .agents import repair as repair_agent

            def _regen(d):
                return generator_agent.generate(d, plan_cfg, run_dir, **_gen_kwargs)

            repair_info = repair_agent.preflight_repair(run_dir, disc, _regen)
            if repair_info and repair_info.get("repaired"):
                gen = generator_agent.generate(disc, plan_cfg, run_dir, **_gen_kwargs)

        # Route the build through the Orchestrator: it runs Review + the new
        # Validation layer and takes bounded decisions (continue / retry / stop /
        # escalate) consulting the KB + Memory. Fully guarded — any failure falls
        # back to a plain review so the run never breaks.
        audit = None
        validation_result = None
        orchestration = None
        try:
            from .agents import (orchestrator as orch_agent,
                                  validation as validation_agent, llm as _llm0)
            _st = {"gen": gen}

            def _gen_step(_facts):
                return _st["gen"]

            def _review_step(g):
                a = reviewer_agent.review(g["script"], disc, plan_cfg, g["do_login"])
                _st["audit"] = a
                return (a.get("failures", 0) == 0), a.get("findings", [])

            def _validate_step(g):
                okv, issues = validation_agent.validate(g["script"], disc, plan_cfg)
                _st["validation"] = {"ok": okv, "issues": issues}
                return okv, issues

            def _repair_step(g, err, use_claude=False):
                ng = generator_agent.generate(disc, plan_cfg, run_dir, **_gen_kwargs)
                _st["gen"] = ng
                return ng

            def _error_of(findings, issues):
                return (validation_agent.summarize(issues)
                        or " ".join(str(f.get("name", "")) for f in (findings or [])
                                    if f.get("status") == "fail"))

            steps = orch_agent.PipelineSteps(
                generate=_gen_step, review=_review_step, repair=_repair_step,
                validate=_validate_step, error_of=_error_of)
            build = orch_agent.Orchestrator(
                target_url=disc["base_url"], claude_available=_llm0.available(),
                max_attempts=2).build_and_validate(steps)
            gen = _st["gen"]
            audit = _st.get("audit")
            validation_result = _st.get("validation")
            orchestration = {"status": build.status, "attempts": build.attempts,
                             "trail": build.trail}
        except Exception as _oe:
            orchestration = {"status": "fallback", "error": str(_oe)}
        if audit is None:
            audit = reviewer_agent.review(gen["script"], disc, plan_cfg, gen["do_login"])

        project_id = db.get_or_create_project(
            req.project_name, disc["base_url"], req.client or "",
            {"test_type": req.test_type, "expected_users": req.expected_users})

        # Closed-loop AI auto-heal: if the run fails, Claude reads the real
        # failures, fixes the script, and it re-runs automatically (bounded).
        from .agents import repair as repair_agent, llm as _llm

        def _heal_cb(rd):
            return repair_agent.post_run_repair(
                rd, disc, lambda d: generator_agent.generate(d, plan_cfg, run_dir, **_gen_kwargs))

        heal_cb = _heal_cb if _llm.available() else None

        # BUSINESS-VALUE PRE-FLIGHT. One user, one journey: does a real, correctly
        # valued order actually happen? A run that returns 200 everywhere while
        # creating 0-value orders reads as a pass in every other section of the
        # report, so this is the only place that catches it. Blocks before the load
        # run, so no worthless orders are written and no time is wasted.
        preflight_gate = repair_agent.preflight_value_gate(run_dir, disc["base_url"])
        if not preflight_gate.get("ok", True):
            raise HTTPException(status_code=409,
                                detail="Pre-flight value gate: "
                                       + str(preflight_gate.get("reason", "")))

        executor_agent.start(run_id, run_dir, disc["base_url"], plan_cfg, project_id,
                             disc, heal_cb=heal_cb)

        return {
            "run_id": run_id,
            "preflight_gate": preflight_gate,
            "plan": plan_cfg,
            "workload": workload,
            "review": audit,
            "validation": validation_result,
            "orchestration": orchestration,
            "repair": repair_info,
            "scripts_index": gen["scripts_index"],
            "script_preview": gen["script"][:4000],
            "discovery": {
                "domain": disc["domain"], "tech": disc["tech"],
                "pages": disc["pages"], "journeys": disc["journeys"],
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500,
                            detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/run/{run_id}/stop")
def run_stop(run_id: str):
    """Abort a running test; a partial report is still produced."""
    ok = executor_agent.stop(run_id)
    return {"stopped": ok}


@app.get("/api/run/{run_id}")
def run_status(run_id: str):
    state = executor_agent.get_state(run_id)
    if state is None:
        stored = db.get_run(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return {"status": stored["status"], "stored": stored, "report_ready": True}
    # trim heavy analysis object for polling; keep summary
    out = {k: v for k, v in state.items() if k != "analysis"}
    if state.get("analysis"):
        out["analysis"] = state["analysis"]
    return out


@app.get("/api/run/{run_id}/calls")
def run_calls(run_id: str, since: int = 0, limit: int = 500):
    """Incremental live feed of individual API calls (JMeter View-Results-Tree
    style). Returns entries after `since` (a line cursor) plus the next cursor and
    the total seen so far, so the UI can append as the run streams."""
    out_dir = (executor_agent.get_state(run_id) or {}).get("output_dir")
    if not out_dir:
        stored = db.get_run(run_id)
        out_dir = stored.get("output_dir") if stored else None
    if not out_dir:
        return {"calls": [], "next": since, "total": 0}
    path = Path(out_dir) / "results" / "apea_calls.jsonl"
    if not path.exists():
        return {"calls": [], "next": since, "total": 0}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return {"calls": [], "next": since, "total": 0}
    total = len(lines)
    since = max(0, int(since or 0))
    cap = max(1, min(int(limit or 500), 2000))
    calls = []
    for ln in lines[since:since + cap]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            calls.append(json.loads(ln))
        except Exception:
            pass
    return {"calls": calls, "next": min(total, since + cap), "total": total}


@app.get("/api/run/{run_id}/report", response_class=HTMLResponse)
def run_report(run_id: str):
    stored = db.get_run(run_id)
    state = executor_agent.get_state(run_id)
    out_dir = None
    if stored and stored.get("output_dir"):
        out_dir = stored["output_dir"]
    if not out_dir and state:
        out_dir = None
    if not out_dir:
        raise HTTPException(status_code=404, detail="Report not found")
    report = Path(out_dir) / "reports" / "performance_report.html"
    if not report.exists():
        raise HTTPException(status_code=404, detail="Report not generated yet")
    return report.read_text(encoding="utf-8")


@app.get("/api/run/{run_id}/report.xlsx")
def run_report_xlsx(run_id: str):
    stored = db.get_run(run_id)
    if not stored or not stored.get("output_dir"):
        raise HTTPException(status_code=404, detail="Report not found")
    xlsx = Path(stored["output_dir"]) / "reports" / "performance_report.xlsx"
    if not xlsx.exists():
        raise HTTPException(status_code=404, detail="Excel report not generated")
    return FileResponse(str(xlsx),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        filename=f"performance_report_{run_id}.xlsx")


@app.get("/api/projects")
def projects():
    return db.list_projects()


@app.get("/api/history")
def history(project_id: Optional[int] = None):
    return db.list_runs(project_id)


@app.get("/api/run/{run_id}/detail")
def run_detail(run_id: str):
    stored = db.get_run(run_id)
    if not stored:
        raise HTTPException(status_code=404, detail="Run not found")
    return stored


# --------------------------------------------------------------------------- #
# saved scripts — build a reusable library, run without re-discovering
# --------------------------------------------------------------------------- #
# A saved script keeps the discovery + data so it can be REGENERATED with the
# current generator at run time (never a stale snapshot).
_SCRIPT_FILES = ("discovery.json", "data/testdata.csv", "scripts/locustfile.py",
                 "endpoints_map.json", "api_inventory.json")


def _copy_artifacts(src: Path, dest: Path):
    for rel in _SCRIPT_FILES:
        s = src / rel
        if s.exists():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(str(s), str(dest / rel))


@app.post("/api/save-script")
def save_script(req: SaveReq):
    slug = slugify(req.name) or "script"
    dest = SAVED_DIR / slug
    dest.mkdir(parents=True, exist_ok=True)
    # Retain the recording so the saved script is self-sufficient for CI export.
    _rec_file = ""
    try:
        if req.recording and Path(req.recording).exists():
            _rec_file = "recording" + Path(req.recording).suffix
            shutil.copy(str(req.recording), str(dest / _rec_file))
    except Exception:
        _rec_file = ""
    try:
        if req.run_id:
            stored = db.get_run(req.run_id)
            if not stored or not stored.get("output_dir"):
                raise HTTPException(status_code=404, detail="Run not found")
            _copy_artifacts(Path(stored["output_dir"]), dest)
            meta = {"name": req.name, "base_url": stored.get("target_url"),
                    "domain": stored.get("domain") or "", "test_type": stored.get("test_type"),
                    "expected_users": stored.get("users") or 100,
                    "users": req.users or stored.get("users"),
                    "duration_s": req.duration_s or stored.get("duration_s"),
                    "workers": req.workers or stored.get("workers") or 1,
                    "recording_file": _rec_file,
                    "created_at": datetime.utcnow().isoformat(), "source_run": req.run_id}
        else:
            if not req.url:
                raise HTTPException(status_code=400, detail="url is required to save after Discover")
            creds = {"username": req.username, "password": req.password} if req.username else None
            base = discovery_agent._norm_base(req.url)
            disc = _DISCOVERY.get(base) or discovery_agent.crawl(req.url, credentials=creds)
            if req.recording:
                recording_agent.merge_into_discovery(disc, recording_agent.parse_recording(req.recording))
            up_users = _extract_users(_read_csv_rows(req.users_csv)[1]) if req.users_csv else []
            up_data = _extract_data(_read_csv_rows(req.data_csv)[1]) if req.data_csv else []
            overrides = {}
            if req.users:
                overrides["users"] = req.users
            if req.duration_s:
                overrides["duration_s"] = req.duration_s
            if req.spawn_rate:
                overrides["spawn_rate"] = req.spawn_rate
            plan_cfg = planner_agent.plan(req.test_type, req.expected_users, req.peak_users,
                                          overrides=overrides)
            plan_cfg["workers"] = max(1, int(req.workers or 1))
            _save_rows = (_read_csv_rows(req.data_csv)[1] if getattr(req, "data_csv", None)
                          else (_read_csv_rows(req.users_csv)[1] if getattr(req, "users_csv", None) else []))
            generator_agent.generate(disc, plan_cfg, dest, credentials=creds,
                                     users=up_users, data=up_data,
                                     cart_qty=getattr(req, "cart_qty", 1) or 1,
                                     captcha_token=getattr(req, "captcha_token", "") or "",
                                     captcha_field=getattr(req, "captcha_field", "") or "",
                                     payment_method=getattr(req, "payment_method", "") or "",
                                     payment_additional_data=getattr(req, "payment_additional_data", "") or "",
                                     data_rows=_save_rows, faithful=bool(getattr(req, "faithful", False)))
            meta = {"name": req.name, "base_url": disc.get("base_url"),
                    "domain": disc.get("domain") or "", "test_type": req.test_type,
                    "expected_users": req.expected_users, "peak_users": req.peak_users,
                    "users": req.users, "duration_s": req.duration_s,
                    "workers": req.workers or 1, "recording_file": _rec_file,
                    "created_at": datetime.utcnow().isoformat()}
        (dest / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return {"saved": True, "name": req.name, "slug": slug}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.get("/api/saved-scripts")
def saved_scripts():
    out = []
    for d in sorted(SAVED_DIR.glob("*")):
        meta_f = d / "meta.json"
        if meta_f.exists():
            try:
                out.append(json.loads(meta_f.read_text(encoding="utf-8")))
            except Exception:
                pass
    return out


@app.post("/api/saved-script-info")
def saved_script_info(req: NameReq):
    """What a saved script already contains, so the UI can disable baked-in inputs
    (recording + data) and show only what still needs to be provided per run."""
    saved = SAVED_DIR / slugify(req.name)
    if not (saved / "meta.json").exists():
        raise HTTPException(status_code=404, detail="Saved script not found")
    present = []
    tf = saved / "data" / "testdata.csv"
    if tf.exists():
        try:
            hdr = _read_csv_rows(tf)[0] or []
            present = [c for c in hdr if c]
        except Exception:
            present = []
    return {
        "has_recording": (saved / "discovery.json").exists()
                          or (saved / "scripts" / "locustfile.py").exists(),
        "has_data": bool(present),
        "data_columns": present,
    }


@app.post("/api/run-saved")
def run_saved(req: RunSavedReq):
    saved = SAVED_DIR / slugify(req.name)
    meta_f = saved / "meta.json"
    if not meta_f.exists():
        raise HTTPException(status_code=404, detail="Saved script not found")
    meta = json.loads(meta_f.read_text(encoding="utf-8"))
    base_url = meta.get("base_url") or ""

    overrides = {}
    if req.users:
        overrides["users"] = req.users
    if req.duration_s:
        overrides["duration_s"] = req.duration_s
    if req.spawn_rate:
        overrides["spawn_rate"] = req.spawn_rate
    test_type = req.test_type or meta.get("test_type", "load")
    expected = req.expected_users or int(meta.get("expected_users", 100) or 100)
    peak = req.peak_users or meta.get("peak_users")
    plan_cfg = planner_agent.plan(test_type, expected, peak, overrides=overrides)
    plan_cfg["workers"] = max(1, int(req.workers or 1))

    if not executor_agent.locust_available():
        raise HTTPException(status_code=400, detail="Locust is not installed.")

    run_id = uuid.uuid4().hex[:12]
    run_dir = project_run_dir(meta.get("name", "saved"), base_url, run_id)

    # Preferred: REGENERATE the script from the saved discovery with the CURRENT
    # generator, so saved scripts always get the latest detection / auto-heal /
    # order-placement. Falls back to replaying a legacy stored locustfile.
    disc_f = saved / "discovery.json"
    disc = {"base_url": base_url, "domain": meta.get("domain", "")}
    target = base_url
    if disc_f.exists():
        disc = json.loads(disc_f.read_text(encoding="utf-8"))
        target = disc.get("base_url") or base_url
        users, data, creds = [], [], None
        tf = saved / "data" / "testdata.csv"
        if tf.exists():
            rows = _read_csv_rows(tf)[1]
            users = _extract_users(rows)
            data = _extract_data(rows)
            if users:
                creds = users[0]
        # Recording-first T&C: harvest checkout agreement_ids from the SAVED
        # recording (the fresh-run path does this; saved runs must too, or the
        # order 400s with "agree to the terms and conditions").
        agreement_ids = disc.get("agreement_ids") or []
        if not agreement_ids:
            _rf = meta.get("recording_file")
            _rp = (saved / _rf) if _rf else next(iter(saved.glob("recording.*")), None)
            if _rp and Path(_rp).exists():
                try:
                    agreement_ids = (recording_agent.parse_recording(str(_rp))
                                     .get("agreement_ids") or [])
                except Exception:
                    agreement_ids = []
        _rs_kwargs = dict(credentials=creds, users=users, data=data,
                          cart_qty=req.cart_qty or 1,
                          captcha_token=req.captcha_token or "",
                          captcha_field=req.captcha_field or "",
                          payment_method=req.payment_method or "",
                          payment_additional_data=req.payment_additional_data or "",
                          agreement_ids=agreement_ids,
                          data_rows=rows, faithful=bool(req.faithful))
        gen = generator_agent.generate(disc, plan_cfg, run_dir, **_rs_kwargs)
    else:
        _copy_artifacts(saved, run_dir)
        gen = None
        _rs_kwargs = None

    script_file = run_dir / "scripts" / "locustfile.py"
    if not script_file.exists():
        raise HTTPException(status_code=400, detail="Saved script could not be prepared")

    # Script review — shown for saved-script runs too (defect fix).
    if gen is None:
        try:
            _src = script_file.read_text(encoding="utf-8")
            gen = {"script": _src, "scripts_index": [], "do_login": "login" in _src.lower()}
        except Exception:
            gen = {"script": "", "scripts_index": [], "do_login": False}
    review = reviewer_agent.review(gen["script"], disc, plan_cfg, gen.get("do_login", False))

    project_id = db.get_or_create_project(meta.get("name", "saved"), target)
    from .agents import repair as repair_agent, llm as _llm
    heal_cb = None
    if _rs_kwargs and _llm.available():
        heal_cb = lambda rd: repair_agent.post_run_repair(
            rd, disc, lambda d: generator_agent.generate(d, plan_cfg, run_dir, **_rs_kwargs))
    # Same business-value gate on the saved-script path (see /api/run).
    preflight_gate = repair_agent.preflight_value_gate(run_dir, target)
    if not preflight_gate.get("ok", True):
        raise HTTPException(status_code=409,
                            detail="Pre-flight value gate: "
                                   + str(preflight_gate.get("reason", "")))
    executor_agent.start(run_id, run_dir, target, plan_cfg, project_id, disc, heal_cb=heal_cb)
    return {"run_id": run_id, "plan": plan_cfg, "name": meta.get("name"),
            "preflight_gate": preflight_gate,
            "review": review, "scripts_index": gen.get("scripts_index", []),
            "script_preview": (gen.get("script") or "")[:4000]}


# --------------------------------------------------------------------------- #
# rename / delete — saved scripts, projects, history runs
# --------------------------------------------------------------------------- #
@app.post("/api/saved-scripts/rename")
def saved_rename(req: RenameReq):
    old = SAVED_DIR / slugify(req.name)
    new = SAVED_DIR / slugify(req.new_name)
    if not old.exists():
        raise HTTPException(status_code=404, detail="Saved script not found")
    if new.exists() and new.resolve() != old.resolve():
        raise HTTPException(status_code=400, detail="A saved script with that name already exists")
    old.rename(new)
    mf = new / "meta.json"
    if mf.exists():
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
            m["name"] = req.new_name
            mf.write_text(json.dumps(m, indent=2), encoding="utf-8")
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/saved-scripts/delete")
def saved_delete(req: NameReq):
    d = SAVED_DIR / slugify(req.name)
    if d.exists():
        shutil.rmtree(str(d), ignore_errors=True)
    return {"ok": True}


@app.post("/api/saved-scripts/export")
def saved_export(req: NameReq):
    """Package a saved script into a committable CI/CD bundle (recording +
    testdata.csv + prefilled perf-test.yml + post-deploy workflow + README)."""
    from . import ci_export
    saved = SAVED_DIR / slugify(req.name)
    if not saved.exists():
        raise HTTPException(status_code=404, detail="Saved script not found")
    out = BASE_DIR / "ci_export" / slugify(req.name)
    res = ci_export.build_bundle(saved, out)
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("error", "export failed"))
    return res


@app.post("/api/projects/rename")
def project_rename(req: ProjectRenameReq):
    db.rename_project(req.project_id, req.new_name)
    return {"ok": True}


@app.post("/api/projects/delete")
def project_delete(req: dict):
    pid = req.get("project_id")
    if pid is None:
        raise HTTPException(status_code=400, detail="project_id is required")
    db.delete_project(int(pid))
    return {"ok": True}


@app.post("/api/runs/rename")
def run_rename(req: RunEditReq):
    db.set_run_label(req.run_id, req.label or "")
    return {"ok": True}


@app.post("/api/runs/delete")
def run_delete(req: RunEditReq):
    db.delete_run(req.run_id)
    return {"ok": True}


@app.post("/api/query")
def query(req: QueryReq):
    """Natural-language interface over the history ledger.

    Always returns the structured ledger rows. When an Anthropic API key is set,
    it also returns a plain-language `answer` from Claude grounded in that data.
    """
    import re
    text = req.text.lower()
    m = re.search(r"(\d+)\s*month", text)
    months = int(m.group(1)) if m else 6
    entity = ""
    for kw in ("checkout", "cart", "login", "search", "home", "product", "category",
               "dashboard", "pricing", "page"):
        if kw in text:
            entity = kw
            break
    rows = db.query_endpoint_history(entity, months)
    regressions_only = "regress" in text or "fail" in text or "breach" in text
    if regressions_only:
        rows = [r for r in rows if not r["sla_pass"]]

    answer = None
    try:
        from .agents import llm
        if llm.available():
            import json as _json
            recent = db.list_runs(limit=40) if hasattr(db, "list_runs") else []
            ctx = {"question": req.text,
                   "matched_entity": entity or "(any)", "window_months": months,
                   "endpoint_history": rows[:80], "recent_runs": recent[:40]}
            answer = llm.chat(
                "Answer the user's question about their performance-test history using "
                "ONLY this data. Be concise and specific; cite numbers. If the data "
                "doesn't contain the answer, say so.\n\n" + _json.dumps(ctx, default=str),
                system="You are APEA, a performance-engineering assistant.",
                max_tokens=800)
    except Exception:
        answer = None

    return {"entity": entity or "(any)", "months": months, "count": len(rows),
            "results": rows[:200], "answer": answer,
            "ai_enabled": _ai_status()["enabled"]}


def _ai_status() -> dict:
    try:
        from .agents import llm
        return llm.status()
    except Exception:
        return {"enabled": False, "model": None, "key_present": False}


@app.get("/api/ai-status")
def ai_status():
    """Report whether the agentic (Claude) layer is active, for the UI badge."""
    return _ai_status()


class AiToggleReq(BaseModel):
    enabled: bool


@app.post("/api/ai-toggle")
def ai_toggle(req: AiToggleReq):
    """Master AI on/off — stop or resume ALL Claude calls (cap API spend) without
    touching the key. Applies immediately to every subsequent run."""
    try:
        from .agents import llm
        llm.set_enabled(bool(req.enabled))
    except Exception:
        pass
    return _ai_status()
