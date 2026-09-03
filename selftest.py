#!/usr/bin/env python3
"""Offline self-test for LT Metrics — validates the pipeline without a live target.

Runs discovery-planning-generation-review against synthetic data, compiles the
generated Locust script, and exercises the analyzer + reporter with fabricated
Locust CSV output. Also boots a tiny local HTTP site and does a real crawl.

    python selftest.py
"""
from __future__ import annotations

import ast
import http.server
import socketserver
import threading
from pathlib import Path

from ltmetrics.agents import discovery, planner, generator, reviewer, analyzer
from ltmetrics import reporting, db
from ltmetrics.config import PROJECTS_DIR

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, extra=""):
    results.append((name, ok))
    print(f"[{PASS if ok else FAIL}] {name} {extra}")


# ---- 1. Local site + real crawl -------------------------------------------
HTML = b"""<html><head><title>Demo Shop</title></head><body>
<a href="/product/1">Product</a><a href="/category/shoes">Category</a>
<a href="/cart">Cart</a><a href="/customer/account/login">Login</a>
<form action="/catalogsearch/result" method="get"><input name="q"></form>
<form action="/customer/account/login" method="post">
<input name="username"><input name="password" type="password">
<input type="hidden" name="form_key" value="ABC123"></form>
<div>add to cart product price checkout basket sku</div></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *a):
        pass


def run_selftest():
    with socketserver.TCPServer(("127.0.0.1", 0), H) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}"

        disc = discovery.crawl(url, max_pages=8)
        check("Discovery reachable", disc["reachable"])
        check("Domain detected as E-commerce", disc["domain"] == "E-commerce",
              f"(got {disc['domain']})")
        check("Search endpoint found", disc["search_endpoint"] is not None)
        check("Journeys mapped", len(disc["journeys"]) >= 1)
        httpd.shutdown()

    # ---- 2. Plan (all test types) ----
    all_ok = True
    for t in planner.ALL_TYPES:
        p = planner.plan(t, 100, 500)
        if not (p["users"] > 0 and p["duration_s"] > 0 and p["spawn_rate"] > 0):
            all_ok = False
    check("Planner produces valid config for all test types", all_ok)

    plan_cfg = planner.plan("load", 100, 500)

    # ---- 3. Generate + compile ----
    run_dir = PROJECTS_DIR / "_selftest" / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    creds = {"username": "u@test.com", "password": "pw"}
    gen = generator.generate(disc, plan_cfg, run_dir, credentials=creds)
    script = gen["script"]
    compile_ok = True
    try:
        ast.parse(script)
        compile(script, "locustfile.py", "exec")
    except SyntaxError as e:
        compile_ok = False
        print("   SyntaxError:", e)
    check("Generated Locust script compiles", compile_ok)
    check("testdata.csv written", Path(gen["testdata_path"]).exists())
    check("Script has catch_response guards", "catch_response=True" in script)
    check("Script has named transactions", script.count("name=") >= 3)
    check("Login correlation present", "_extract_token" in script)

    # ---- 4. Review ----
    audit = reviewer.review(script, disc, plan_cfg, gen["do_login"])
    check("Reviewer verdict APPROVED", audit["verdict"] == "APPROVED",
          f"({audit['score']})")

    # ---- 5. Analyzer + reporter with fabricated Locust CSV ----
    (run_dir / "results").mkdir(exist_ok=True)
    stats_csv = (
        "Type,Name,Request Count,Failure Count,Median Response Time,"
        "Average Response Time,Min Response Time,Max Response Time,"
        "Average Content Size,Requests/s,Failures/s,"
        "50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%\n"
        "GET,Home Page,1000,2,120,140,50,900,1024,25.5,0.05,120,150,180,200,300,450,600,700,850,900,900\n"
        "GET,Search Results,400,20,900,1100,300,6000,2048,10.1,0.5,900,1200,1500,1800,3000,5200,5800,5900,6000,6000,6000\n"
        ",Aggregated,1400,22,300,420,50,6000,1200,35.6,0.55,300,500,800,1000,1800,3400,5000,5500,6000,6000,6000\n"
    )
    (run_dir / "results" / "locust_stats.csv").write_text(stats_csv, encoding="utf-8")
    hist_csv = (
        "Timestamp,User Count,Type,Name,Requests/s,Failures/s,"
        "50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%,"
        "Total Request Count,Total Failure Count,Total Median Response Time,"
        "Total Average Response Time,Total Min Response Time,Total Max Response Time,"
        "Total Average Content Size\n"
        "1710000000,50,,Aggregated,20,0.2,300,400,500,600,900,1600,2000,2200,3000,3000,3000,700,4,300,420,50,3000,1200\n"
        "1710000010,100,,Aggregated,35,0.5,320,450,600,700,1200,3400,5000,5500,6000,6000,6000,1400,22,300,420,50,6000,1200\n"
    )
    (run_dir / "results" / "locust_stats_history.csv").write_text(hist_csv, encoding="utf-8")
    failures_csv = "Method,Name,Error,Occurrences\nGET,Search Results,HTTPError 500,20\n"
    (run_dir / "results" / "locust_failures.csv").write_text(failures_csv, encoding="utf-8")

    db.init_db()
    pid = db.get_or_create_project("_selftest", disc["base_url"])
    rid = "selftest01"
    db.create_run(rid, pid, "load", 100, 10, 600, str(run_dir))
    analysis = analyzer.analyze(run_dir, plan_cfg, disc, pid, rid)
    check("Analyzer overall metrics", analysis["overall"]["total_requests"] == 1400)
    check("Analyzer computes error rate", round(analysis["overall"]["error_rate"], 2) == 1.57,
          f"(got {analysis['overall']['error_rate']})")
    check("SLA gate evaluated", isinstance(analysis["sla"]["pass"], bool))
    check("Recommendations generated", len(analysis["recommendations"]) >= 1)
    check("RCA summary present", bool(analysis["rca"]["summary"]))

    rep = reporting.build_reports(analysis, run_dir)
    check("HTML report written", Path(rep["html"]).exists())
    check("Excel report written", rep["xlsx"] is not None and Path(rep["xlsx"]).exists())

    db.finalize_run(rid, {
        "total_requests": analysis["overall"]["total_requests"],
        "total_failures": analysis["overall"]["total_failures"],
        "error_rate": analysis["overall"]["error_rate"],
        "avg_response": analysis["overall"]["avg_response"],
        "p95": analysis["overall"]["p95"],
        "throughput": analysis["overall"]["throughput"],
        "sla_pass": analysis["sla"]["pass"],
    })
    db.save_endpoints(rid, analysis["endpoints"])
    hist = db.query_endpoint_history("search", 6)
    check("Ledger history query works", len(hist) >= 1)

    # ---- 6. Distributed command builders + CLI parser ----
    from ltmetrics.agents import executor
    single = executor._single_cmd(plan_cfg, disc["base_url"])
    master = executor._master_cmd(plan_cfg, disc["base_url"], 4)
    worker = executor._worker_cmd()
    check("Single-process command built", "--headless" in single and "--csv" in single)
    check("Master command built", "--master" in master and "--expect-workers" in master)
    check("Worker command built", "--worker" in worker and "--master-host" in worker)

    from ltmetrics import cli
    ns = cli.build_parser().parse_args(
        ["--url", "https://example.com", "--test-type", "stress",
         "--workers", "4", "--check-sla"])
    check("CLI parses arguments", ns.url == "https://example.com"
          and ns.test_type == "stress" and ns.workers == 4 and ns.check_sla)

    # ---- 7. Application-aware modules (this session's additions) ----
    try:
        from ltmetrics.agents import (test_plan_analyzer, performance_planner,
                                 payment_replay, payment_analyzer, business_data,
                                 application_knowledge, test_data_generator, live_store,
                                 data_pools)
        from ltmetrics import memory
        mods_ok = True
    except Exception as e:                       # a syntax/import error here is a hard fail
        mods_ok = False
        print("   import error:", e)
    check("Application-aware modules import", mods_ok)

    if mods_ok:
        # Test Plan Analyzer — keyword fallback (no API key) on inline text.
        tp = test_plan_analyzer.analyze(text=(
            "Objective: validate checkout sustains peak Black Friday traffic.\n"
            "Target environment: https://staging.example.com\n"
            "Business scenarios: login, search, add to cart, checkout, payment.\n"
            "Smoke test: 10 concurrent users for 5 minutes.\n"
            "Load test: 500 users for 30 minutes, ramp-up 5 minutes, ramp-down 2 minutes.\n"
            "Stress test: 5000 users.\n"
            "Think time 3 to 8 seconds.\n"
            "Pass criteria: p95 response time under 3000 ms, error rate below 1%."))
        _types = set(tp.get("available_test_types") or [])
        check("Test Plan Analyzer finds test types", {"smoke", "load", "stress"} <= _types,
              f"(got {sorted(_types)})")
        check("Test Plan Analyzer parses user counts",
              any(t.get("users") == 500 for t in tp.get("tests", [])))
        check("Test Plan Analyzer parses objectives", len(tp.get("objectives") or []) >= 1)
        check("Test Plan Analyzer parses environment",
              (tp.get("target_environment") or "").startswith("http"))
        check("Test Plan Analyzer parses business scenarios",
              len(tp.get("business_scenarios") or []) >= 1)
        check("Test Plan Analyzer emits coverage map", isinstance(tp.get("coverage"), dict))
        check("Test Plan Analyzer never errors", tp.get("source") != "error")

        # Performance Planner — execution-plan + overrides + merge into plan_cfg.
        ep = performance_planner.build(tp, "load",
                                       business_flow={"name": "checkout", "steps": ["login", "cart"]})
        ov = performance_planner.to_overrides(ep)
        pc = dict(plan_cfg)
        performance_planner.apply_to_plan_cfg(pc, ep)
        check("Performance Planner builds execution-plan", ep.get("test_type") == "load")
        check("Performance Planner emits overrides", ov.get("users") == 500)
        check("Performance Planner merges SLA/think", "exit_criteria" in pc)
        _pth = planner.plan("load", 100, 500, overrides={"think_min": 7, "think_max": 9})
        check("Planner applies think-time override", _pth.get("think_time") == [7, 9])
        _pc2 = {"think_time": [7, 9]}
        performance_planner.apply_to_plan_cfg(_pc2, ep, skip_think=True)
        check("apply_to_plan_cfg respects skip_think (explicit override wins)",
              _pc2.get("think_time") == [7, 9])

        # Payment analyzers — safe on empty; classify a synthetic Stripe capture.
        check("payment_replay None on empty", payment_replay.classify({}) is None)
        _cap = {"gateway": "stripe", "payment_network": [
            {"method": "POST", "url": "https://api.stripe.com/v1/payment_methods",
             "status": 200, "post_data": "card"}]}
        _pr = payment_replay.classify(_cap)
        check("payment_replay classifies stripe", bool(_pr) and _pr.get("gateway") == "stripe")
        check("payment_analyzer safe on empty", payment_analyzer.analyze({}, {}) is None)
        check("business_data safe on empty", business_data.discover({}, {}) is None)
        check("memory.learn_order_signal safe on None",
              memory.learn_order_signal("http://x", None) is None)

        # Application Knowledge Base — assemble from the crawled discovery.
        akb = application_knowledge.build(disc, target_url=disc.get("base_url", ""))
        check("Application KB builds with platform", bool(akb.get("platform")))
        check("Application KB has navigation", isinstance(akb.get("navigation"), dict)
              and bool(akb["navigation"].get("home") or akb["navigation"].get("search")))
        check("Application KB has api_surface", isinstance(akb.get("api_surface"), dict))
        check("Application KB emits coverage map", isinstance(akb.get("coverage"), dict))
        check("Application KB refresh seam works",
              application_knowledge.refresh(disc.get("base_url", ""), disc).get("source") == "refresh")

        # Intelligent Test Data Generator — realistic rows + payment intelligence.
        _gd = test_data_generator.generate(
            akb, test_plan=tp, execution_plan=ep,
            business_flow={"steps": ["login", "checkout", "paradoxlabs_cybersource"]},
            existing_rows=[{"username": "u@test.com", "password": "pw", "sku": "ABC",
                            "search_keyword": "widget",
                            "payment_method": "paradoxlabs_cybersource", "country_id": "US"}],
            out_dir=None, options={"customer_mode": "existing"})
        check("Test Data Generator produces rows", _gd.get("rows", 0) >= 1)
        check("Test Data Generator distributes SKUs", _gd.get("unique_skus", 0) >= 1)
        check("Test Data Generator card-if-recorded", _gd.get("payment", {}).get("is_card") is True)
        check("Test Data Generator emits suggested_config", isinstance(_gd.get("suggested_config"), dict))
        check("Test Data Generator has validation gate", isinstance(_gd.get("validation"), dict))

        # Live-store provider — pure/degradation paths only (no network in selftest).
        check("live_store derives store code", live_store.store_from_paths(
            ["/uk/rest/uk/V1/carts/mine"]) == "uk")
        check("live_store unsupported platform degrades",
              live_store.discover_products("http://x", platform="shopify").get("source") == "unsupported")

        # Multi-pool data detection — classify crawl URLs, bind to transactions,
        # honor weighted selection. Pure/offline (no network in selftest).
        check("data_pools classifies PDP url",
              data_pools.classify_url("/uk/buy/allen-bradley-1771-oad/14635554.html") == "pdp")
        check("data_pools classifies manufacturer url",
              data_pools.classify_url("/uk/manufacturer/3amechatronic") == "manufacturer")
        check("data_pools classifies paginated PLP url",
              data_pools.classify_url("/uk/sensors/level-transmitters.html?p=2") == "plp")
        _dp_disc = {"base_url": "https://x", "pages": [
            {"name": "PDP", "path": "/uk/buy/x-brand-a/123.html"},
            {"name": "Brand", "path": "/uk/manufacturer/acme"},
            {"name": "Cat", "path": "/uk/sensors/transmitters.html?p=3"}]}
        _dp_flow = [{"group": "View PDP", "path": "/uk/buy/x-brand-a/123.html"},
                    {"group": "Brand browse", "path": "/uk/manufacturer/acme"}]
        _dp = data_pools.detect(_dp_disc, flow_steps=_dp_flow,
                                live_products={"products": [{"sku": "S1", "price": 9.9, "name": "Widget"}]})
        check("data_pools builds pdp pool", _dp.get("coverage", {}).get("pdp", 0) >= 1)
        check("data_pools binds a transaction group",
              any(b.get("pool") == "pdp" for b in _dp.get("bindings", [])))
        check("data_pools weighted expansion repeats by weight",
              len(data_pools.weighted_values(
                  {"weight_field": "w", "values": [{"t": "a", "w": "3"}, {"t": "b", "w": "1"}]})) == 4)

        # ---- 8. Regenerate with the new options — exercises the new template
        #         placeholders (__PAY_API__, __DATA_SHARING__, __FAITHFUL_FORCED_OFF__,
        #         __ORDER_URL_SIGNALS__, __ORDER_PLACE_PATTERNS__) and compiles the output.
        disc2 = dict(disc)
        disc2["flow"] = [
            {"method": "POST", "path": "/v1/payment_methods", "hdrs": {}, "asserts": [],
             "body": '{"card":{"number":"4242424242424242"}}', "resp": '{"id":"pm_selftest"}'},
            {"method": "POST", "path": "/api/orders", "hdrs": {}, "asserts": [],
             "body": '{"payment_method_id":"pm_selftest"}', "resp": '{"ok":true}'},
        ]
        pc2 = dict(plan_cfg)
        pc2["data_sharing"] = "unique"
        pc2["payment_api_replay"] = True
        rd2 = PROJECTS_DIR / "_selftest" / "run2"
        rd2.mkdir(parents=True, exist_ok=True)
        _ok2 = True
        try:
            gen2 = generator.generate(disc2, pc2, rd2, credentials=creds, faithful=True)
            compile(gen2["script"], "locustfile2.py", "exec")
            _script2 = gen2["script"]
        except Exception as e:
            _ok2 = False
            _script2 = ""
            print("   generate/compile error:", e)
        check("Script compiles with unique + api-replay + faithful", _ok2)
        check("API-replay config baked into script", "token_request" in _script2)
        check("Data-sharing mode baked into script", "_DATA_SHARING" in _script2)

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok in results if ok)
    print(f"  {passed}/{len(results)} checks passed")
    print("=" * 50)
    return passed == len(results)


if __name__ == "__main__":
    ok = run_selftest()
    raise SystemExit(0 if ok else 1)
