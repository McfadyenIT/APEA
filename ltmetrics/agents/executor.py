"""Execution & Monitoring Agent.

Runs the generated Locust script headless as a subprocess, streams live
aggregate stats to an in-memory registry (so the UI can poll progress), then
hands the raw results to the analyzer + reporter and records the run in the
SQLite ledger.

Supports both single-process and distributed (master + N workers) execution,
and both threaded (web UI) and blocking (CLI / CI) invocation.
"""
from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import db

# run_id -> live state dict
_RUNS: dict[str, dict] = {}
# run_id -> (master_proc, [worker_procs]) so a run can be aborted mid-flight
_PROCS: dict[str, tuple] = {}
_LOCK = threading.Lock()

RESULTS_PREFIX = "results/locust"


def stop(run_id: str) -> bool:
    """Abort a running test. Locust writes stats periodically, so a partial
    report is still produced from whatever completed before the stop."""
    _set(run_id, stopped=True, phase="Stopping — finalizing partial results…")
    entry = _PROCS.get(run_id)
    if not entry:
        return False
    proc, workers = entry
    for p in [proc] + list(workers or []):
        try:
            if p and p.poll() is None:
                p.terminate()
        except Exception:
            pass
    return True


def kill_all() -> int:
    """Terminate every tracked Locust process (used before a server restart).
    Returns the number of processes signalled."""
    n = 0
    for rid, entry in list(_PROCS.items()):
        if not entry:
            continue
        proc, workers = entry
        for p in [proc] + list(workers or []):
            try:
                if p and p.poll() is None:
                    p.terminate()
                    n += 1
            except Exception:
                pass
    return n


def get_state(run_id: str) -> dict | None:
    with _LOCK:
        st = _RUNS.get(run_id)
        return dict(st) if st else None


def _set(rid: str, **kw) -> None:
    with _LOCK:
        _RUNS.setdefault(rid, {}).update(kw)


def locust_available() -> bool:
    from .engines import get_engine
    return get_engine("locust").available()


def _module_present() -> bool:
    try:
        import locust  # noqa: F401
        return True
    except Exception:
        return False


def _launcher() -> list[str]:
    locust_bin = shutil.which("locust")
    return [locust_bin] if locust_bin else [sys.executable, "-m", "locust"]


def _prepare(run_id, run_dir, plan_cfg, project_id) -> None:
    _set(run_id,
         run_id=run_id, status="starting", phase="Launching Locust",
         users=plan_cfg["users"], target_users=plan_cfg["users"],
         workers=int(plan_cfg.get("workers", 1)),
         duration_s=plan_cfg["duration_s"], elapsed_s=0,
         total_requests=0, total_failures=0, rps=0.0, p95=0.0,
         error_rate=0.0, log_tail=[], report_ready=False)
    db.create_run(run_id, project_id, plan_cfg["test_type"], plan_cfg["users"],
                  plan_cfg["spawn_rate"], plan_cfg["duration_s"], str(run_dir))


def start(run_id, run_dir: Path, target_url: str, plan_cfg: dict,
          project_id: int, discovery: dict, heal_cb=None, heal_max: int = 2) -> None:
    """Launch the test in a background thread (used by the web UI).

    heal_cb(run_dir) -> dict{"repaired": bool, "diagnosis": str}: optional closed
    loop. Called when a run fails; if it repairs+regenerates, the test re-runs.
    """
    _prepare(run_id, run_dir, plan_cfg, project_id)
    t = threading.Thread(target=run_blocking,
                         args=(run_id, run_dir, target_url, plan_cfg, project_id,
                               discovery, False, heal_cb, heal_max),
                         daemon=True)
    t.start()


def _execute_once(run_id, run_dir, target_url, plan_cfg, log_path) -> bool:
    """Launch Locust (single or distributed), monitor, wait. Returns False on a
    hard launch exception."""
    workers = int(plan_cfg.get("workers", 1))
    try:
        worker_procs: list[subprocess.Popen] = []
        with open(log_path, "w", encoding="utf-8") as logf:
            if workers > 1:
                proc = subprocess.Popen(_master_cmd(plan_cfg, target_url, workers),
                                        cwd=str(run_dir), stdout=logf,
                                        stderr=subprocess.STDOUT, text=True)
                for _ in range(workers):
                    worker_procs.append(subprocess.Popen(
                        _worker_cmd(), cwd=str(run_dir),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True))
            else:
                proc = subprocess.Popen(_single_cmd(plan_cfg, target_url),
                                        cwd=str(run_dir), stdout=logf,
                                        stderr=subprocess.STDOUT, text=True)
            _PROCS[run_id] = (proc, worker_procs)
            _monitor(run_id, run_dir, proc, log_path, plan_cfg)
            proc.wait()
        for wp in worker_procs:
            try:
                wp.wait(timeout=10)
            except Exception:
                wp.kill()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        _set(run_id, status="error", phase="Execution failed", error=str(exc))
        db.set_run_status(run_id, "error")
        return False


def _run_failed(analysis) -> bool:
    """Did the run fail badly enough to auto-heal? SLA breach, or a checkout that
    logged in but never created an order."""
    if not analysis:
        return True
    try:
        if not analysis.get("sla", {}).get("pass", True):
            return True
        flow = analysis.get("flow") or {}
        if (flow.get("login_ok") or flow.get("login_fail")) and not flow.get("orders"):
            return True
    except Exception:
        pass
    return False


def _launch_failed(analysis) -> bool:
    """True if the script barely ran (0 requests) — a broken script, safe to
    quickly re-run after a heal. A full-duration run that produced traffic is NOT
    a launch failure, so we never silently re-run (and extend) a timed test."""
    try:
        return int((analysis or {}).get("overall", {}).get("total_requests", 0) or 0) == 0
    except Exception:
        return False


def run_blocking(run_id, run_dir, target_url, plan_cfg, project_id, discovery,
                 prepared: bool = True, heal_cb=None, heal_max: int = 2) -> dict | None:
    """Run the test synchronously, with an optional AI auto-heal + re-run loop.
    Returns the final analysis dict (or None on a hard error)."""
    if prepared:
        _prepare(run_id, run_dir, plan_cfg, project_id)

    log_path = run_dir / "results" / "locust_run.log"
    workers = int(plan_cfg.get("workers", 1))
    if not locust_available():
        _set(run_id, status="error", phase="Locust not installed",
             error="Locust is not installed. Run: pip install -r requirements.txt")
        db.set_run_status(run_id, "error")
        return None

    # Track B (real-browser) runs ALONGSIDE the Locust track, only when explicitly
    # opted in AND Playwright is installed. Inert otherwise — the default path
    # below is unchanged.
    _browser = _maybe_start_browser_track(run_id, run_dir, target_url, plan_cfg, discovery)

    attempt = 0
    while True:
        _set(run_id, status="running", started=time.time(),
             phase=(("Load test in progress (%d worker(s))" % workers) if workers > 1
                    else "Load test in progress")
                   + (" — auto-heal attempt %d" % (attempt + 1) if attempt else ""))
        if not _execute_once(run_id, run_dir, target_url, plan_cfg, log_path):
            _join_browser_track(_browser, run_id, plan_cfg, run_dir); _browser = None
            return None
        _join_browser_track(_browser, run_id, plan_cfg, run_dir); _browser = None
        _maybe_ai_launch_heal(run_id, run_dir, log_path)
        _set(run_id, status="analyzing", phase="Analyzing results")
        analysis = _finalize(run_id, run_dir, plan_cfg, project_id, discovery)

        stopped = (get_state(run_id) or {}).get("stopped")
        if (heal_cb and not stopped and attempt < heal_max and _run_failed(analysis)):
            _set(run_id, status="healing",
                 phase="AI auto-heal: analyzing failures and regenerating the script…")
            try:
                info = heal_cb(run_dir) or {}
            except Exception as exc:
                info = {"repaired": False, "reason": str(exc)}
            note = info.get("diagnosis") or info.get("reason")
            # Only auto re-run for a LAUNCH failure (0 requests — near-instant).
            # A timed run that produced traffic must NOT be silently re-run, or the
            # test would blow past its configured duration.
            if info.get("repaired") and _launch_failed(analysis):
                attempt += 1
                _set(run_id, ai_heal_note=note,
                     phase="AI auto-heal applied — re-running (attempt %d)…" % (attempt + 1))
                continue
            # No re-run for a timed run — restore the finished status so the UI
            # doesn't stay stuck on "healing", and note the fix for the next run.
            if info.get("repaired"):
                _set(run_id, status="completed",
                     phase="Done — AI applied fixes; run again to validate",
                     ai_heal_note=(note or "") + "  — Fixes applied and the script was "
                     "regenerated. Not auto-re-run, so the test respects its duration.")
            else:
                _set(run_id, status="completed",
                     phase="Done", ai_heal_note=note)
        return analysis


def _maybe_ai_launch_heal(run_id, run_dir, log_path) -> None:
    """Runtime safety net: if the Locust script crashed at launch (traceback in the
    log), get an AI explanation + fix so the failure is actionable. Guarded; no-op
    without an API key. Never on the per-request hot path."""
    try:
        tail = "\n".join(_tail(Path(log_path), 80))
        if "Traceback (most recent call last)" not in tail:
            return
        note = None
        try:
            from . import llm
            if llm.available():
                note = llm.chat(
                    "This Locust load-test script failed to launch. From this "
                    "traceback, give the root cause and the exact fix, briefly.\n\n"
                    + tail[-4000:],
                    system="You debug Python/Locust launch failures.", max_tokens=500)
        except Exception:
            note = None
        _set(run_id, launch_error=True,
             ai_heal_note=note or "Script raised an exception at launch — see locust_run.log.")
        try:
            (Path(run_dir) / "launch_heal.txt").write_text(
                note or tail[-4000:], encoding="utf-8")
        except Exception:
            pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# command builders
# --------------------------------------------------------------------------- #
def _common_args(plan_cfg, target_url) -> list[str]:
    return [
        "-f", "scripts/locustfile.py",
        "--host", target_url,
        "--headless",
        "-u", str(plan_cfg["users"]),
        "-r", str(plan_cfg["spawn_rate"]),
        "--run-time", f"{plan_cfg['duration_s']}s",
        "--csv", RESULTS_PREFIX,
        "--csv-full-history",
        "--html", "results/locust_raw.html",
        "--only-summary",
        "--loglevel", "INFO",
    ]


def _engine(plan_cfg):
    from .engines import get_engine
    return get_engine((plan_cfg or {}).get("engine", "locust"))


def _single_cmd(plan_cfg, target_url) -> list[str]:
    return _engine(plan_cfg).single_cmd(plan_cfg, target_url)


def _master_cmd(plan_cfg, target_url, workers) -> list[str]:
    return _engine(plan_cfg).master_cmd(plan_cfg, target_url, workers)


def _worker_cmd() -> list[str]:
    return _engine({}).worker_cmd()


# --------------------------------------------------------------------------- #
# monitoring + finalize
# --------------------------------------------------------------------------- #
def _maybe_start_browser_track(run_id, run_dir, target_url, plan_cfg, discovery):
    """Opt-in real-browser (Playwright) track, launched alongside Locust. Returns a
    (proc, logfile) handle or None. Fully guarded: if the flag is off, Playwright
    isn't installed, or anything fails, it no-ops and the Locust run is unaffected."""
    bt = (plan_cfg or {}).get("browser_track") or {}
    if not bt.get("enabled"):
        return None
    try:
        from .engines import get_engine
        eng = get_engine("playwright")
        if not eng.available():
            _set(run_id, browser_track="skipped — Playwright not installed "
                 "(pip install playwright && playwright install chromium)")
            return None
        from . import browser_runner_gen
        runner = browser_runner_gen.generate_browser_runner(
            run_dir, target_url, plan_cfg, discovery or {})
        if not runner:
            _set(run_id, browser_track="skipped — no browser journey to run")
            return None
        (run_dir / "results").mkdir(parents=True, exist_ok=True)
        logf = open(run_dir / "results" / "playwright_run.log", "w", encoding="utf-8")
        proc = subprocess.Popen(eng.single_cmd(plan_cfg, target_url), cwd=str(run_dir),
                                stdout=logf, stderr=subprocess.STDOUT, text=True)
        _set(run_id, browser_track="running — %d browser VUs" % int(bt.get("vus") or 0))
        return (proc, logf)
    except Exception as exc:  # never let Track B break the run
        _set(run_id, browser_track="error — %s" % exc)
        return None


def _join_browser_track(handle, run_id, plan_cfg, run_dir=None) -> None:
    """Wait for the browser track to finish (bounded by test duration + buffer),
    then surface its summary. No-op when Track B wasn't started."""
    if not handle:
        return
    proc, logf = handle
    timeout = int((plan_cfg or {}).get("duration_s") or 60) + 120
    try:
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        try:
            logf.close()
        except Exception:
            pass
    note = "done"
    try:
        if run_dir is not None:
            import json as _json
            s = _json.loads((Path(run_dir) / "results" / "browser_track.json")
                            .read_text(encoding="utf-8"))
            note = ("done — %s iteration(s), %s request(s), %s failure(s)%s"
                    % (s.get("iterations", 0), s.get("requests", 0), s.get("failures", 0),
                       (" — " + s["note"]) if s.get("note") and s["note"] != "ok" else ""))
            _pr = s.get("prices") or []
            if _pr:
                note += " — real price seen: %s" % _pr[0].get("price", "")
            # Fail-closed: surface whether the selected card payment actually
            # cleared, so _finalize can FAIL the run instead of masking it.
            _preq = bool(s.get("payment_required"))
            _pok = bool(s.get("payment_ok"))
            _set(run_id, browser_payment_required=_preq, browser_payment_ok=_pok,
                 browser_payment_err=s.get("payment_err") or "",
                 browser_payment_gateway=s.get("gateway") or "")
            if _preq and not _pok:
                note += " — CARD PAYMENT FAILED (run marked failed)"
    except Exception:
        pass
    _set(run_id, browser_track=note)


def _monitor(run_id, run_dir, proc, log_path, plan_cfg) -> None:
    history = run_dir / "results" / "locust_stats_history.csv"
    load_start = None                       # wall-clock anchor at FIRST real traffic
    while proc.poll() is None:
        time.sleep(2)
        live = _read_history_tail(history)
        live.update(_read_flow_stats(run_dir))
        # ELAPSED = duration of the actual LOAD phase only, NOT the process/pipeline
        # wall-clock. Prefer Locust's own stats-history timestamps (the true traffic
        # window); fall back to counting from the first request seen. Either way this
        # excludes Locust boot / user-spawn before traffic, and — because the loop
        # ends when the Locust process exits — it never counts generation, analysis,
        # or AI-heal time.
        elapsed = _history_span(history)
        if elapsed is None:
            if load_start is None and (live.get("total_requests") or 0) > 0:
                load_start = time.time()
            elapsed = int(time.time() - load_start) if load_start else 0
        tail = _tail(log_path, 12)
        _set(run_id, elapsed_s=elapsed, log_tail=tail,
             pct=min(99, int(elapsed * 100 / max(1, plan_cfg["duration_s"]))),
             **live)


def _history_span(path: Path):
    """Elapsed seconds of the LOAD phase, taken from the Locust stats-history
    timestamps: (last aggregated sample) - (first aggregated sample). Returns None
    until there's a sample to anchor to, or if the file can't be read."""
    if not path.exists():
        return None
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            ts = [float(r["Timestamp"]) for r in csv.DictReader(fh)
                  if r.get("Name") == "Aggregated" and r.get("Timestamp")]
    except Exception:
        return None
    if not ts:
        return None
    return max(0, int(ts[-1] - ts[0]))


def _read_flow_stats(run_dir: Path) -> dict:
    """Live login / order counters written by the generated flow script."""
    import json
    path = Path(run_dir) / "results" / "ltm_flow.json"
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return {"login_ok": d.get("login_ok", 0), "login_fail": d.get("login_fail", 0),
                "orders": d.get("orders", 0), "timeline": d.get("timeline", []),
                "mode": d.get("mode", ""), "build": d.get("build", ""),
                "checkout_state": d.get("checkout_state", {}),
                "applied_memory": d.get("applied_memory", {})}
    except Exception:
        return {}


def _read_history_tail(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        return {}
    agg = [r for r in rows if r.get("Name") == "Aggregated"]
    if not agg:
        return {}
    last = agg[-1]
    return {
        "users": _num(last.get("User Count")),
        "rps": _num(last.get("Requests/s")),
        "p95": _num(last.get("95%")),
        "total_requests": _num(last.get("Total Request Count")),
        "total_failures": _num(last.get("Total Failure Count")),
    }


def _price_heal(run_dir, discovery, analysis) -> None:
    """Auto-heal: if the run ADDED items at price 0 (a client-side-priced catalog
    where the REST/catalog price is 0), resolve the REAL price from the storefront
    HTML so a run never silently records a £0 product. Post-run, off the hot path,
    never raises."""
    import re as _re
    try:
        flow = analysis.get("flow") or {}
        timeline = flow.get("timeline") or []
        zero = any('"price":0' in str(s.get("body") or "").replace(" ", "")
                   for s in timeline
                   if "items" in str(s.get("url") or "").lower())
        if not (zero and flow.get("orders")):
            return
        base = (discovery or {}).get("base_url") or ""
        term = store = sku = None
        # The sku to price is the one that actually LANDED IN THE CART. Read it
        # from a SUCCESSFUL add-to-cart RESPONSE: scanning every request instead
        # picks up failed (out-of-stock) attempts and prices a different product,
        # which then reports as a bogus browser-vs-API "mismatch".
        for s in timeline:
            if not s.get("ok") or "items" not in str(s.get("url") or "").lower():
                continue
            m = _re.search(r'"sku"\s*:\s*"([^"]+)"', str(s.get("body") or ""))
            if m:
                sku = m.group(1)
                break
        for s in timeline:
            if term is None:
                m = _re.search(r"term='([^']+)'", str(s.get("extra") or ""))
                if m:
                    term = m.group(1)
            if sku is None:
                m = _re.search(r'"sku":\s*"([^"]+)"', str(s.get("req") or ""))
                if m:
                    sku = m.group(1)
            if store is None:
                m = _re.search(r"//[^/]+/([^/]+)/rest/", str(s.get("url") or ""))
                if m:
                    store = m.group(1)
        # product_id from the data — its digits usually appear in the PDP url, so
        # it's the best hint to price the EXACT ordered product (not a lookalike).
        pid = None
        try:
            import csv as _csv
            with open(Path(run_dir) / "data" / "testdata.csv", newline="",
                      encoding="utf-8") as fh:
                for row in _csv.DictReader(fh):
                    pid = (row.get("product_id") or "").strip() or pid
                    if pid:
                        break
        except Exception:
            pid = None
        from . import price_resolver
        res = price_resolver.resolve(base, sku or term or pid or "", store=store,
                                     match_hints=[sku, pid, term])
        if not res:
            analysis["price_heal"] = {"resolved": False, "for_sku": sku, "note":
                "items priced 0 by the API (client-side-priced catalog); the real "
                "price could not be scraped from the HTML — the browser track reads it"}
            return
        # for_sku records WHICH product this price belongs to, so the business-data
        # gate can refuse to compare it against a different product's API price.
        analysis["price_heal"] = {"resolved": True, "for_sku": sku, **res}
        analysis["recommendations"] = ([{"priority": "P1",
            "title": "Catalog price is 0 — real price resolved from the storefront",
            "detail": ("The API added the product at price 0 (client-side-priced "
                       "catalog). LT Metrics resolved the real price %s%s from %s. The REST "
                       "order value reflects shipping/tax only; use this for true "
                       "order value." % (res.get("currency", ""), res.get("price"),
                                          res.get("url")))}]
            + list(analysis.get("recommendations", [])))
    except Exception:
        pass


def _finalize(run_id, run_dir, plan_cfg, project_id, discovery) -> dict | None:
    from . import analyzer
    from .. import reporting

    try:
        analysis = analyzer.analyze(run_dir, plan_cfg, discovery, project_id, run_id)
    except Exception as exc:  # pragma: no cover
        # Even if analysis fails, still emit a minimal report so the run is never
        # left with nothing (raw results remain intact for manual inspection).
        import traceback as _tb
        try:
            reporting.build_reports(
                {"overall": {}, "sla": {}, "flow": _read_flow_stats(run_dir),
                 "browser_track": None, "endpoints": [], "recommendations": [],
                 "_analysis_error": str(exc)}, run_dir)
        except Exception:
            pass
        _set(run_id, status="error", phase="Analysis failed", error=str(exc),
             report_ready=True)
        db.set_run_status(run_id, "error")
        return None

    # Recommendation step — prepend KB/RCA/checkout-grounded recommendations to
    # the analyzer's performance ones (SAME shape). Merge, never overwrite, so the
    # UI/report renderers keep working.
    try:
        from . import recommendation
        kb_recs = recommendation.build(analysis, discovery, run_dir)
        if kb_recs:
            analysis["recommendations"] = kb_recs + list(analysis.get("recommendations", []))
    except Exception:
        pass

    # Price auto-heal: resolve the real price when the API added items at £0
    # (client-side-priced catalog). Never blocks; enriches the report.
    _price_heal(run_dir, discovery, analysis)

    # Business Data Dependency + Browser-vs-API fidelity: classify business values
    # (price today) and flag any resolved-in-browser-but-0-in-API mismatch.
    try:
        from . import business_data
        _bd = business_data.discover(discovery, analysis)
        if _bd:
            analysis["business_data"] = _bd
            for _r in _bd.get("recommendations", []):
                analysis["recommendations"] = [_r] + list(analysis.get("recommendations", []))
    except Exception:
        pass

    st = get_state(run_id) or {}
    stopped = st.get("stopped")
    metrics = analysis["overall"]

    # Payment replay-profile: from the captured payment network sequence (Track B)
    # + KB replay_profiles, decide whether the card step is HTTP-replayable. Pure
    # analysis — enriches the report, never blocks. Inert when Track B didn't run.
    try:
        from . import payment_replay
        _bt = (analysis.get("browser_track") or {}).get("summary") or {}
        _pr = payment_replay.classify(_bt, detected_gateway=st.get("browser_payment_gateway") or "")
        if _pr:
            analysis["payment_replay"] = _pr
    except Exception:
        pass

    # Payment Analyzer: payment as a first-class dependency. Emits a Payment Profile
    # (provider, integration type, token endpoint+field, auto token correlation, and
    # the chosen execution strategy). Pure analysis; enriches the report.
    try:
        from . import payment_analyzer
        _pp = payment_analyzer.analyze(discovery, analysis,
                                       detected_gateway=st.get("browser_payment_gateway") or "")
        if _pp:
            analysis["payment_profile"] = _pp
    except Exception:
        pass

    # Fail-closed payment gate: if a card gateway was SELECTED but the browser
    # track's card payment did not clear, the run FAILS — the SLA is failed and no
    # other payment method was substituted. This is what stops a run from being
    # reported "completed" when the card never actually went through.
    # Both tracks publish the SAME fail-closed contract, so a declared card
    # payment that did not happen fails the run whichever track was driving it.
    # The HTTP track raises this when the test data declares a card gateway but a
    # user's row cannot supply the token: falling back to an offline method there
    # would report a pass for a card journey that never ran.
    # _read_flow_stats() filters to the live counters, so read the raw flow file
    # for the payment keys the generated script publishes.
    try:
        import json as _json
        _fp = Path(run_dir) / "results" / "ltm_flow.json"
        _http_pay = _json.loads(_fp.read_text(encoding="utf-8")) if _fp.exists() else {}
    except Exception:
        _http_pay = {}
    _http_req = bool((_http_pay or {}).get("payment_required"))
    _http_ok = bool((_http_pay or {}).get("payment_ok"))
    payment_failed = (
        (bool(st.get("browser_payment_required")) and not bool(st.get("browser_payment_ok")))
        or (_http_req and not _http_ok))
    sla_pass = bool(analysis["sla"]["pass"])
    if payment_failed:
        if _http_req and not _http_ok:
            reason = ("Declared card payment (%s) could not be presented: %s "
                      "Run marked FAILED — no fallback payment method was substituted."
                      % ((_http_pay or {}).get("gateway") or "card gateway",
                         (_http_pay or {}).get("payment_err") or "no card token for this user."))
        else:
            reason = ("Selected card payment (%s) did not clear in the browser track: %s. "
                      "Run marked FAILED — no fallback payment method was substituted."
                      % (st.get("browser_payment_gateway") or "hosted gateway",
                         st.get("browser_payment_err") or "card form not reachable"))
        analysis["payment_gate"] = {"required": True, "ok": False, "reason": reason}
        analysis["sla"]["pass"] = False
        sla_pass = False
        analysis["recommendations"] = ([{"priority": "P0",
            "title": "Card payment did not clear", "detail": reason}]
            + list(analysis.get("recommendations", [])))

    if stopped:
        final_status = "stopped"
    elif payment_failed:
        final_status = "failed"
    else:
        final_status = "completed"

    db.finalize_run(run_id, {
        "total_requests": metrics["total_requests"],
        "total_failures": metrics["total_failures"],
        "error_rate": metrics["error_rate"],
        "avg_response": metrics["avg_response"],
        "p95": metrics["p95"],
        "throughput": metrics["throughput"],
        "sla_pass": sla_pass,
    }, status=final_status)
    db.save_endpoints(run_id, analysis["endpoints"])

    try:
        reporting.build_reports(analysis, run_dir)
        report_ready = True
    except Exception as exc:  # pragma: no cover
        report_ready = False
        _set(run_id, report_error=str(exc))

    _phase = ("Stopped (partial results)" if stopped
              else "FAILED — card payment did not clear" if payment_failed
              else "Done")
    _set(run_id, status=final_status,
         phase=_phase, pct=100,
         report_ready=report_ready, analysis=analysis,
         total_requests=metrics["total_requests"],
         total_failures=metrics["total_failures"],
         error_rate=metrics["error_rate"], p95=metrics["p95"],
         rps=metrics["throughput"], **_read_flow_stats(run_dir))

    # Memory layer — Store/Learn: turn this run into knowledge the next run reuses
    # (e.g. target -> region_policy=omit). Deterministic, best-effort, never fatal.
    try:
        from .. import memory
        target = (discovery or {}).get("base_url") or (discovery or {}).get("domain") or ""
        learnings = memory.learn_from_run_dir(target, run_dir, run_id=run_id,
                                              project_id=project_id)
        if learnings:
            _set(run_id, learnings=learnings)
    except Exception:
        pass

    # Close the crawl->assertion loop: persist the browser-observed order-success
    # signal so the NEXT generation asserts order completion the way this store
    # confirms it (facts_for -> _applied_memory -> generated _ORDER_URL_SIGNALS).
    try:
        from .. import memory
        _tgt = (discovery or {}).get("base_url") or (discovery or {}).get("domain") or ""
        _oc = ((analysis.get("browser_track") or {}).get("summary") or {}).get("order_confirmation")
        _learned = memory.learn_order_signal(_tgt, _oc, run_id=run_id)
        if _learned:
            _set(run_id, learnings=[_learned])
    except Exception:
        pass

    # Run-artifact history — persist the FINAL script + errors + root cause so
    # 'generated vs final' can be diffed and script-level fixes mined over time.
    try:
        from .. import memory
        rd = Path(run_dir)
        final_script = None
        try:
            final_script = (rd / "scripts" / "locustfile.py").read_text(encoding="utf-8")
        except Exception:
            pass
        errors = _read_failures_rows(rd)
        flow = _read_flow_stats(run_dir)
        root_cause = ((flow.get("checkout_state") or {}).get("stop_reason") or "")
        db.save_run_artifacts(run_id, project_id=project_id,
                              platform=memory.detect_platform(
                                  (discovery or {}).get("base_url", "")),
                              final_script=final_script, errors=errors,
                              root_cause=root_cause)
    except Exception:
        pass
    return analysis


def _read_failures_rows(run_dir: Path, limit: int = 50) -> list[dict]:
    """Compact list of the run's failures for history (method, name, error, count)."""
    p = Path(run_dir) / "results" / "locust_failures.csv"
    if not p.exists():
        return []
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        return []
    return [{"method": r.get("Method", ""), "name": r.get("Name", ""),
             "error": (r.get("Error", "") or "")[:500],
             "count": r.get("Occurrences", "")} for r in rows[:limit]]


def _tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return [ln.rstrip() for ln in fh.readlines()[-n:]]
    except FileNotFoundError:
        return []


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
