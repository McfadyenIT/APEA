"""Offline self-test for LT Metrics's Knowledge + Memory + Orchestrator layers.

Runs with NO server and NO network. From the project root:

    .venv\\Scripts\\activate.bat        (Windows)   or   source .venv/bin/activate
    python test_knowledge_layer.py

Exits 0 if every check passes, 1 otherwise. Uses a throwaway target host so it
never pollutes your real project data.
"""
import sys

from ltmetrics.knowledge import KB
from ltmetrics import db, memory
from ltmetrics.agents import orchestrator as orch

_fails = 0


def check(name, cond, detail=""):
    global _fails
    ok = bool(cond)
    if not ok:
        _fails += 1
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "   << " + str(detail)))
    return ok


print("\n1) Knowledge Base loads")
mag = KB.platform_rules("magento")
check("platform rules present", bool(mag), "empty — is PyYAML installed?")
check("regionId is integer rule", mag.get("region_id_is_integer") is True, mag.get("region_id_is_integer"))
check("correlation library non-empty", len(KB.correlations()) >= 3, len(KB.correlations()))
check("known bugs catalogued", len(KB.known_bugs()) >= 5, len(KB.known_bugs()))
check("business flow model present", len(KB.business_flow("magento")) >= 6, len(KB.business_flow("magento")))
check("resolves products via GraphQL", KB.magento("resolve_via") == "graphql", KB.magento("resolve_via"))

print("\n2) Deterministic error classification (the real Radwell failures)")
cases = {
    "The product that was requested doesn't exist. Verify the product and try again.": "product_not_found",
    "Product that you are trying to add is not available.": "product_out_of_stock",
    "The requested Payment Method is not available.": "payment_method_unavailable",
    "The shipping method can't be set for an empty cart. Add an item to cart and try again.": "empty_cart_at_shipping",
    "token status 200 body=eyJraWQiOiIxIiwiYWxnIjoiSFMyNTYifQ.eyJ...": "jwt_token_rejected",
    "Resolve: product 401": "product_lookup_admin_401",
}
for text, expect in cases.items():
    bug = KB.classify_error(text)
    check("classify -> " + expect, bug and bug.get("id") == expect,
          "got " + str(bug and bug.get("id")))

print("\n3) Deterministic repair lookup (no LLM)")
check("out-of-stock -> stop", (KB.repair_for("Product ... you are trying to add is not available") or {}).get("action") == "stop_checkout_out_of_stock")
check("product-not-found -> search", (KB.repair_for("the product that was requested doesn't exist") or {}).get("action") == "resolve_product_via_search")
check("payment -> offline method", (KB.repair_for("the requested payment method is not available") or {}).get("action") == "select_offline_payment_from_methods")

print("\n4) Memory learn -> recall (Run N teaches Run N+1, no LLM)")
db.init_db()
host = "https://kb-selftest.example/uk"
flow_143 = {"orders": 0, "build": "selftest",
            "checkout_state": {"stopped_at": "Set shipping information",
                               "stop_reason": "regionId invalid — sent NUTS 1 Code string, not integer"}}
learned = memory.learn_from_flow(host, flow_143, run_id="run-143")
check("run 143 recorded a learning", any("region" in str(x) for x in learned), learned)
facts = memory.facts_for(host)          # what run 144's generator would read
check("run 144 knows region policy", facts.get("region_policy") == "omit_region_id_unless_integer", facts.get("region_policy"))
# a later run that SUCCEEDS teaches the winning payment method -> next run prefers it
memory.learn_from_flow(host, {"orders": 1, "build": "t2", "winning_payment": "checkmo",
                              "checkout_state": {}}, run_id="run-200")
check("success learns payment method", memory.facts_for(host).get("payment_method") == "checkmo",
      memory.facts_for(host).get("payment_method"))

print("\n5) Decision Engine (retry / stop / escalate)")
de = orch.DecisionEngine(claude_available=False)
check("known non-terminal -> RETRY", de.decide("the requested payment method is not available", 1, 3).action == orch.RETRY)
check("out-of-stock -> STOP", de.decide("you are trying to add is not available", 1, 3).action == orch.STOP)
check("unknown + no LLM -> ESCALATE", de.decide("some entirely novel error 9f3", 1, 3).action == orch.ESCALATE)
de2 = orch.DecisionEngine(claude_available=True)
check("unknown + LLM -> INVOKE_CLAUDE", de2.decide("some entirely novel error 9f3", 1, 3).action == orch.INVOKE_CLAUDE)
check("payment routing avoids hosted gateway",
      de.payment_policy("magento", ["paradoxlabs_cybersource", "checkmo"]).get("method") == "checkmo")

print("\n6) New layers — Validation / Execution Engine / Recommendation")
from ltmetrics.agents import validation as _val
from ltmetrics.agents.engines import get_engine, available_engines
from ltmetrics.agents import recommendation as _rec
_bad = "def x(:\n  pass"            # deliberately broken
check("validation flags a syntax error", not _val.validate(_bad)[0])
_good = ("from locust import HttpUser, task, between\n"
         "class U(HttpUser):\n  wait_time=between(1,2)\n"
         "  @task\n  def t(self):\n"
         "    with self.client.get('/', catch_response=True) as r: r.success()\n")
check("validation passes a clean script", _val.validate(_good)[0], _val.summarize(_val.validate(_good)[1]))
check("engine registry -> locust default", get_engine("nope").name == "locust", get_engine("nope").name)
check("k6/jmeter registered", set(["locust", "k6", "jmeter"]) <= set(available_engines().keys()))
recs = _rec.build({"overall": {"error_rate": 5.0, "p95": 100}, "endpoints": [], "sla": {}})
check("recommendation flags high error rate (UI shape)",
      any("error rate" in r.get("title", "").lower() for r in recs)
      and all({"priority", "title", "impact", "effort", "detail"} <= set(r) for r in recs), recs)

print("\n7) Platform registry (#6) + run-artifact history (#10)")
from ltmetrics import platforms
check("detects Magento from REST path",
      platforms.detect({"flow": [{"path": "/uk/rest/uk/V1/carts/mine"}]}) == "magento")
check("detects Shopify from cart/add.js",
      platforms.detect({"flow": [{"path": "/cart/add.js"}]}) == "shopify")
check("magento checkout mode = rest-checkout", platforms.checkout_mode("magento") == "rest-checkout")
check("shopify checkout mode = recorded-replay", platforms.checkout_mode("shopify") == "recorded-replay")
db.save_run_artifacts("kb-selftest-run", generated_script="print('gen')")
db.save_run_artifacts("kb-selftest-run", final_script="print('final')", root_cause="regionId")
_a = db.get_run_artifacts("kb-selftest-run") or {}
check("run artifacts persist generated + final",
      _a.get("generated_script") == "print('gen')" and _a.get("final_script") == "print('final')", _a)

print("\n" + ("ALL CHECKS PASSED" if not _fails else f"{_fails} CHECK(S) FAILED"))
sys.exit(1 if _fails else 0)
