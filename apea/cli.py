"""Headless CLI runner — the CI/CD entry point for APEA.

Runs the full pipeline (discover -> plan -> generate -> review -> execute ->
analyze -> report) without the web UI, prints a summary, and (optionally) exits
non-zero when the SLA/quality gate fails so it can gate a pipeline.

Examples
--------
    python -m apea.cli --url https://example.com --test-type load \\
        --expected-users 100 --check-sla

    python -m apea.cli --url https://example.com --test-type stress \\
        --users 500 --duration 900 --workers 4 --check-sla

Environment variables (fallbacks when a flag is omitted) — handy in CI:
    APEA_TARGET_URL, APEA_TEST_TYPE, APEA_USERS, APEA_DURATION,
    APEA_WORKERS, APEA_USERNAME, APEA_PASSWORD, APEA_PROJECT
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import uuid

from . import db
from .config import project_run_dir, slugify
from .agents import discovery as discovery_agent
from .agents import planner as planner_agent
from .agents import generator as generator_agent
from .agents import reviewer as reviewer_agent
from .agents import executor as executor_agent
from .agents import recording as recording_agent


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# --- uploaded-file parsing (flexible column detection) ----------------------
def _read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig", errors="ignore") as fh:
        reader = csv.DictReader(fh)
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                for r in reader]


def _pick(row, keys):
    for k in keys:
        if row.get(k):
            return row[k]
    return ""


def _extract_users(rows):
    out = []
    for r in rows:
        u = _pick(r, ["username", "user", "email", "userid", "user_id", "login"])
        p = _pick(r, ["password", "pass", "pwd", "passwd"])
        if u or p:
            out.append({"username": u, "password": p})
    return out


def _extract_data(rows):
    out = []
    for r in rows:
        pid = _pick(r, ["product_id", "productid", "product", "sku", "pid", "item_id", "itemid"])
        kw = _pick(r, ["search_keyword", "keyword", "search", "query", "term", "q",
                       "searchterm", "ndc", "name", "product_name", "title"])
        if not pid and not kw:
            for v in r.values():
                if v:
                    kw = v
                    break
        if pid or kw:
            out.append({"product_id": pid, "search_keyword": kw})
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="apea.cli",
                                description="Headless APEA performance test runner")
    p.add_argument("--url", default=_env("APEA_TARGET_URL"),
                   help="Target URL (or APEA_TARGET_URL)")
    p.add_argument("--project", default=_env("APEA_PROJECT"),
                   help="Project name (defaults to the URL host)")
    p.add_argument("--client", default=_env("APEA_CLIENT", ""))
    p.add_argument("--test-type", default=_env("APEA_TEST_TYPE", "load"),
                   choices=planner_agent.ALL_TYPES)
    p.add_argument("--expected-users", type=int,
                   default=int(_env("APEA_EXPECTED_USERS", "100")))
    p.add_argument("--peak-users", type=int, default=None)
    p.add_argument("--users", type=int, default=_int_env("APEA_USERS"))
    p.add_argument("--duration", type=int, default=_int_env("APEA_DURATION"),
                   help="Duration in seconds (overrides the profile default)")
    p.add_argument("--spawn-rate", type=float, default=None)
    p.add_argument("--workers", type=int, default=int(_env("APEA_WORKERS", "1")),
                   help="Distributed Locust workers (master + N workers)")
    p.add_argument("--cart-qty", type=int, default=int(_env("APEA_CART_QTY", "1")),
                   help="Units added per add-to-cart (raise product load)")
    p.add_argument("--captcha-token", default=_env("APEA_CAPTCHA_TOKEN"),
                   help="CAPTCHA bypass / reCAPTCHA test-key response to inject")
    p.add_argument("--captcha-field", default=_env("APEA_CAPTCHA_FIELD"),
                   help="Extra field name to carry the CAPTCHA token")
    p.add_argument("--ai-repair", action="store_true",
                   default=_env("APEA_AI_REPAIR", "") not in ("", "0", "false", "False"),
                   help="AI pre-flight self-repair (needs ANTHROPIC_API_KEY)")
    p.add_argument("--no-ai", action="store_true",
                   default=_env("APEA_LLM_ENABLED", "1") in ("0", "false", "False", "off", "no"),
                   help="Disable ALL Claude/Anthropic calls for this run (cap API "
                        "spend); APEA runs fully deterministic. Key can stay configured.")
    p.add_argument("--payment-method", default=_env("APEA_PAYMENT_METHOD"),
                   help="Force this payment method code at place-order (gateway test mode)")
    p.add_argument("--payment-additional-data", default=_env("APEA_PAYMENT_ADDL"),
                   help="JSON merged into paymentMethod.additional_data (stored-card/test token)")
    p.add_argument("--data-sharing", default=_env("APEA_DATA_SHARING", "all_threads"),
                   choices=["all_threads", "unique"],
                   help="CSV->thread sharing: all_threads (shared pool, recycle) or unique (distinct per user)")
    p.add_argument("--payment-api-replay", action="store_true",
                   help="Generate API-replay payment: mint a sandbox token per VU + correlate it into the order (opt-in)")
    p.add_argument("--faithful", action="store_true",
                   default=_env("APEA_FAITHFUL", "") not in ("", "0", "false", "False"),
                   help="JMeter-style verbatim replay (no parameterize/correlate/heal)")
    p.add_argument("--strict", action="store_true",
                   default=_env("APEA_STRICT", "") not in ("", "0", "false", "False"),
                   help="Reproducible mode: no self-heal, no payment auto-switch, fail "
                        "loud (comparable measurements across runs)")
    p.add_argument("--include-static", action="store_true",
                   default=_env("APEA_INCLUDE_STATIC", "") not in ("", "0", "false", "False"),
                   help="keep static assets/pages (default: exclude, API calls only)")
    p.add_argument("--browser-vus", type=int, default=int(_env("APEA_BROWSER_VUS", "0") or 0),
                   help="real-browser (Playwright) track virtual users; 0 = off (default)")
    p.add_argument("--browser-payment", default=_env("APEA_BROWSER_PAYMENT", ""),
                   help='drive a gateway payment on the browser track: a gateway name '
                        '("stripe") or JSON, e.g. \'{"gateway":"stripe","submit_selector":"#pay"}\'')
    p.add_argument("--username", default=_env("APEA_USERNAME"))
    p.add_argument("--password", default=_env("APEA_PASSWORD"))
    p.add_argument("--users-csv", default=_env("APEA_USERS_CSV"),
                   help="Path to a credentials CSV (username, password columns)")
    p.add_argument("--data-csv", default=_env("APEA_DATA_CSV"),
                   help="Path to a data CSV (product_id, search_keyword columns)")
    p.add_argument("--recording", default=_env("APEA_RECORDING"),
                   help="Path to a recorded script (.jmx / .yaml / .har)")
    p.add_argument("--max-pages", type=int, default=12)
    p.add_argument("--render-js", action="store_true",
                   default=_env("APEA_RENDER_JS", "") not in ("", "0", "false", "False"),
                   help="Force a headless-browser (Playwright) re-crawl during discovery "
                        "for JS-rendered sites. Without this flag, it still auto-engages "
                        "if the static crawl looks JS-heavy and sparse AND Playwright is "
                        "installed — this flag just forces it even when that heuristic "
                        "doesn't trigger. Requires `pip install playwright && "
                        "playwright install chromium`.")
    p.add_argument("--check-sla", action="store_true",
                   help="Exit with code 1 if the SLA/quality gate fails")
    p.add_argument("--config", default=_env("APEA_CONFIG"),
                   help="Plain-English YAML config (perf-test.yml) — sets all options")
    p.add_argument("--export-ci", default=None, metavar="SAVED_NAME",
                   help="Export a saved script into a committable CI/CD bundle and exit")
    return p


# Plain-English config key -> argparse attribute. Lets CI use natural language.
_CONFIG_KEYMAP = {
    "target url": "url", "url": "url", "target": "url",
    "recording file": "recording", "recording": "recording",
    "test type": "test_type", "test": "test_type",
    "users": "users", "concurrent users": "users",
    "expected users": "expected_users", "peak users": "peak_users",
    "duration": "duration", "run time": "duration", "runtime": "duration",
    "workers": "workers", "load generators": "workers",
    "data file": "data_csv", "data csv": "data_csv",
    "credentials file": "users_csv", "users file": "users_csv",
    "credentials csv": "users_csv",
    "cart quantity": "cart_qty", "cart qty": "cart_qty",
    "payment method": "payment_method",
    "payment additional data": "payment_additional_data",
    "captcha bypass token": "captcha_token", "captcha token": "captcha_token",
    "project": "project", "client": "client",
    "username": "username", "password": "password",
    "ai self repair": "ai_repair", "ai repair": "ai_repair",
    "check sla": "check_sla", "fail on sla": "check_sla",
    "faithful": "faithful", "verbatim replay": "faithful",
    "include static": "include_static", "keep static": "include_static",
}
_INT_KEYS = {"users", "expected_users", "peak_users", "workers", "cart_qty"}
_BOOL_KEYS = {"ai_repair", "check_sla", "faithful", "include_static"}


def _parse_duration(v):
    """'10m' / '1h' / '600s' / 600  -> seconds (int)."""
    m = re.match(r"^\s*(\d+)\s*([smh]?)\s*$", str(v).lower())
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    return n * 3600 if u == "h" else n * 60 if u == "m" else n


def _to_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def _apply_config(args, path: str) -> None:
    """Merge a plain-English YAML config file onto the parsed args."""
    import yaml
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    for raw_key, val in cfg.items():
        if val is None or str(val).strip() == "":
            continue
        key = str(raw_key).strip().lower()
        # tolerate "pass if error rate below" / "pass if p95 below" -> enable gate
        if key.startswith("pass if"):
            args.check_sla = True
            continue
        attr = _CONFIG_KEYMAP.get(key)
        if not attr:
            continue
        if attr == "duration":
            d = _parse_duration(val)
            if d:
                args.duration = d
        elif attr in _INT_KEYS:
            try:
                setattr(args, attr, int(val))
            except (TypeError, ValueError):
                pass
        elif attr in _BOOL_KEYS:
            setattr(args, attr, _to_bool(val))
        else:
            setattr(args, attr, str(val))


def _int_env(name):
    v = os.environ.get(name)
    try:
        return int(v) if v else None
    except ValueError:
        return None


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "no_ai", False):        # master AI toggle — no Claude calls
        try:
            from .agents import llm as _llm0
            _llm0.set_enabled(False)
        except Exception:
            pass
    if getattr(args, "export_ci", None):
        from . import ci_export
        from .config import SAVED_DIR, BASE_DIR, slugify
        saved = SAVED_DIR / slugify(args.export_ci)
        if not saved.exists():
            print("ERROR: saved script not found: %s" % args.export_ci, file=sys.stderr)
            return 2
        out = BASE_DIR / "ci_export" / slugify(args.export_ci)
        res = ci_export.build_bundle(saved, out)
        if not res.get("ok"):
            print("ERROR: %s" % res.get("error"), file=sys.stderr)
            return 2
        print("[APEA] CI bundle written to: %s" % res["out_dir"])
        for f in res["files"]:
            print("   - %s" % f)
        for w in res.get("warnings", []):
            print("   ! %s" % w)
        return 0
    if getattr(args, "config", None):
        try:
            _apply_config(args, args.config)
        except Exception as exc:
            print(f"ERROR: could not read config {args.config}: {exc}", file=sys.stderr)
            return 2
    # Recording-first: derive the target URL from the recording if not given.
    if args.recording and not args.url:
        try:
            args.url = recording_agent.parse_recording(args.recording).get("base_url")
        except Exception:
            pass
    if not args.url:
        print("ERROR: provide a recording (recording file:) or a --url", file=sys.stderr)
        return 2
    if not executor_agent.locust_available():
        print("ERROR: Locust is not installed. Run: pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    db.init_db()
    creds = {"username": args.username, "password": args.password} if args.username else None
    project = args.project or slugify(args.url)

    # Recording-first: build discovery from the recording; crawl only as a fallback.
    _agreement_ids = []
    _rec_creds = {}
    if args.recording:
        rec = recording_agent.parse_recording(
            args.recording, include_static=bool(getattr(args, "include_static", False)))
        disc = {"base_url": rec.get("base_url") or args.url, "domain": "Recorded",
                "tech": [], "pages": [], "forms": [], "apis": [], "journeys": [],
                "reachable": True, "status_code": 200, "notes": []}
        recording_agent.merge_into_discovery(disc, rec)
        # Mirror the server path: carry the recording-harvested T&C agreement ids
        # through to the generator. merge_into_discovery does NOT propagate these,
        # so without this the CLI would place orders with no agreement_ids and
        # Magento would reject them (the REST agreements endpoint often 404s).
        _agreement_ids = rec.get("agreement_ids") or []
        if _agreement_ids and not disc.get("agreement_ids"):
            disc["agreement_ids"] = _agreement_ids
        # capture recording-typed credentials for the browser track (login)
        for _si in (rec.get("selenium_inputs") or []):
            _f = str(_si.get("field") or "").lower()
            _v = _si.get("sample") or _si.get("value")
            if _v and _f == "username":
                _rec_creds["username"] = _v
            elif _v and _f == "password":
                _rec_creds["password"] = _v
        print(f"[APEA] Recording: {len(rec.get('endpoints', []))} endpoint(s) from "
              f"{rec.get('source')}" + (f" — {rec['error']}" if rec.get("error") else ""))
    else:
        print(f"[APEA] Discovering {args.url} …")
        disc = discovery_agent.crawl(args.url, max_pages=args.max_pages, credentials=creds,
                                     render_js=(True if args.render_js else None))
        print(f"       domain={disc['domain']}  pages={len(disc['pages'])}  "
              f"tech={', '.join(disc['tech']) or 'unknown'}")
    up_users = _extract_users(_read_csv_rows(args.users_csv)) if args.users_csv else []
    up_data = _extract_data(_read_csv_rows(args.data_csv)) if args.data_csv else []
    if up_users:
        print(f"       users CSV: {len(up_users)} credential row(s)")
    if up_data:
        print(f"       data CSV: {sum(1 for d in up_data if d['search_keyword'])} keyword(s), "
              f"{sum(1 for d in up_data if d['product_id'])} product id(s)")

    overrides = {}
    if args.users:
        overrides["users"] = args.users
    if args.duration:
        overrides["duration_s"] = args.duration
    if args.spawn_rate:
        overrides["spawn_rate"] = args.spawn_rate
    plan_cfg = planner_agent.plan(args.test_type, args.expected_users,
                                  args.peak_users, overrides=overrides)
    plan_cfg["workers"] = max(1, args.workers)
    plan_cfg["data_sharing"] = getattr(args, "data_sharing", "all_threads") or "all_threads"
    plan_cfg["strict"] = bool(getattr(args, "strict", False))   # reproducible mode
    if getattr(args, "payment_api_replay", False):
        plan_cfg["payment_api_replay"] = True
    if int(getattr(args, "browser_vus", 0) or 0) > 0:   # opt-in real-browser track
        _bt = {"enabled": True, "vus": int(args.browser_vus)}
        _bp = (getattr(args, "browser_payment", "") or "").strip()
        if _bp:
            try:
                _bt["payment"] = json.loads(_bp) if _bp.startswith("{") else {"gateway": _bp}
            except Exception:
                _bt["payment"] = {"gateway": _bp}
        # let the browser track log in + reach checkout (explicit creds, else the
        # first CSV user, else credentials captured from the recording itself)
        _bcred = creds or (up_users[0] if up_users else None) or (_rec_creds or None)
        if _bcred:
            _bt["credentials"] = _bcred
        plan_cfg["browser_track"] = _bt
    print(f"[APEA] Plan: {plan_cfg['summary']}")

    run_id = uuid.uuid4().hex[:12]
    run_dir = project_run_dir(project, disc["base_url"], run_id)
    _cli_rows = []
    try:
        if getattr(args, "data_csv", None):
            _cli_rows = _read_csv_rows(args.data_csv)
        elif getattr(args, "users_csv", None):
            _cli_rows = _read_csv_rows(args.users_csv)
    except Exception:
        _cli_rows = []
    _gk = dict(credentials=creds, users=up_users, data=up_data,
               cart_qty=getattr(args, "cart_qty", 1) or 1,
               captcha_token=getattr(args, "captcha_token", "") or "",
               captcha_field=getattr(args, "captcha_field", "") or "",
               payment_method=getattr(args, "payment_method", "") or "",
               payment_additional_data=getattr(args, "payment_additional_data", "") or "",
               data_rows=_cli_rows, faithful=bool(getattr(args, "faithful", False)),
               agreement_ids=_agreement_ids)
    gen = generator_agent.generate(disc, plan_cfg, run_dir, **_gk)
    try:
        db.save_run_artifacts(run_id, generated_script=gen.get("script"))
    except Exception:
        pass
    if getattr(args, "ai_repair", False):
        try:
            from .agents import repair as repair_agent
            info = repair_agent.preflight_repair(
                run_dir, disc, lambda d: generator_agent.generate(d, plan_cfg, run_dir, **_gk))
            if info.get("repaired"):
                gen = generator_agent.generate(disc, plan_cfg, run_dir, **_gk)
                print("[APEA] AI self-repair applied: %s" % info.get("reason"))
            elif info.get("ran"):
                print("[APEA] AI self-repair: %s" % info.get("reason"))
        except Exception as _e:
            print("[APEA] AI self-repair skipped: %s" % _e)
    # Route the build through the Orchestrator (Review + Validation + bounded
    # decisions, KB/Memory-aware). Guarded fallback to a plain review.
    audit = None
    try:
        from .agents import (orchestrator as _orch, validation as _val,
                             llm as _llm0)
        _st = {"gen": gen}

        def _rev(g):
            a = reviewer_agent.review(g["script"], disc, plan_cfg, g["do_login"])
            _st["audit"] = a
            return (a.get("failures", 0) == 0), a.get("findings", [])

        def _rep(g, err, use_claude=False):
            _st["gen"] = generator_agent.generate(disc, plan_cfg, run_dir, **_gk)
            return _st["gen"]

        _steps = _orch.PipelineSteps(
            generate=lambda _f: _st["gen"], review=_rev, repair=_rep,
            validate=lambda g: _val.validate(g["script"], disc, plan_cfg),
            error_of=lambda f, i: _val.summarize(i))
        _build = _orch.Orchestrator(target_url=disc["base_url"],
                                    claude_available=_llm0.available(),
                                    max_attempts=2).build_and_validate(_steps)
        gen = _st["gen"]
        audit = _st.get("audit")
        _v = _val.validate(gen["script"], disc, plan_cfg)
        print("[APEA] Orchestrator: %s (%d attempt(s)) · validation %s"
              % (_build.status, _build.attempts, "OK" if _v[0] else "issues=%d" % len(_v[1])))
    except Exception as _oe:
        print("[APEA] Orchestrator fallback: %s" % _oe)
    if audit is None:
        audit = reviewer_agent.review(gen["script"], disc, plan_cfg, gen["do_login"])
    print(f"[APEA] Script review: {audit['verdict']} ({audit['score']})")

    project_id = db.get_or_create_project(project, disc["base_url"], args.client or "")

    print(f"[APEA] Running {plan_cfg['label']} "
          f"({plan_cfg['users']} users, {plan_cfg['duration_human']}, "
          f"{plan_cfg['workers']} worker(s)) …")
    from .agents import repair as repair_agent, llm as _llm
    _cli_heal = None
    if _llm.available():
        _cli_heal = lambda rd: repair_agent.post_run_repair(
            rd, disc, lambda d: generator_agent.generate(d, plan_cfg, run_dir, **_gk))
    analysis = executor_agent.run_blocking(run_id, run_dir, disc["base_url"],
                                           plan_cfg, project_id, disc, prepared=True,
                                           heal_cb=_cli_heal)
    if analysis is None:
        state = executor_agent.get_state(run_id) or {}
        print(f"ERROR: execution failed — {state.get('error', 'unknown')}",
              file=sys.stderr)
        return 2

    _print_summary(analysis, run_dir)

    if args.check_sla and not analysis["sla"]["pass"]:
        print("\n[APEA] QUALITY GATE: FAIL ❌")
        return 1
    print("\n[APEA] QUALITY GATE: PASS ✅"
          if analysis["sla"]["pass"] else "\n[APEA] SLA breached (gate not enforced)")
    return 0


def _print_summary(analysis: dict, run_dir) -> None:
    o = analysis["overall"]
    sla = analysis["sla"]
    print("\n" + "=" * 60)
    print("  APEA RESULTS")
    print("=" * 60)
    print(f"  Requests    : {o['total_requests']:,}")
    print(f"  Failures    : {o['total_failures']:,} ({o['error_rate']}%)")
    print(f"  Throughput  : {o['throughput']} req/s")
    print(f"  Avg / p95   : {o['avg_response']} ms / {o['p95']} ms")
    print(f"  SLA gate    : {'PASS' if sla['pass'] else 'FAIL'} "
          f"(err<={sla['max_error_rate_pct']}% p95<={int(sla['max_p95_ms'])}ms)")
    print(f"  Report      : {run_dir}/reports/performance_report.html")
    print("  Top recommendations:")
    for r in analysis["recommendations"][:3]:
        print(f"    [{r['priority']}] {r['title']}")


if __name__ == "__main__":
    raise SystemExit(main())
