"""Regression guard for the three measurement fixes.

    ./.venv/bin/python tests_measurement.py     # expect: FAILURES: 0

Each closes a defect confirmed against the codebase on 2026-08-28:

  1. Ramp-up was inside every percentile. Statistics covered the whole run from
     the first spawned user, so a quoted p95 blended the ramp with the steady
     state. Negligible on a 3-minute smoke test; dominant on a 20-minute ramp,
     where early uncontended samples pull the number down.

  2. There was no end-to-end transaction time. Every timing was one HTTP
     request, so "how long does checkout take" could not be answered from the
     report -- even though the recording already grouped its steps into journey
     stages and carried that grouping into the generated script unused.

  3. Throughput could not be held. Pacing was think time only, making throughput
     an OUTPUT of the run: as the system slowed, users completed fewer
     iterations, so offered load fell exactly when the system was stressed.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from ltmetrics.agents.generator import _assemble_flow_script, _wait_time_expr  # noqa: E402
from ltmetrics.agents.engines.locust_engine import LocustEngine               # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print("   %-64s %-6s %s" % (label[:64], "OK" if ok else "FAIL",
                                "" if ok else "(got %r want %r)" % (got, want)))
    if not ok:
        fails.append(label)


DISCOVERY = {
    "base_url": "https://shop.example.com",
    "login_form": {"action": "/uk/customer/account/loginPost"},
    "flow": [
        {"name": "Home", "method": "GET", "path": "/", "rest": False, "group": "Home page"},
        {"name": "Login", "method": "POST", "path": "/uk/customer/account/loginPost",
         "rest": False, "group": "Login"},
        {"name": "Add to cart", "method": "POST", "path": "/uk/checkout/cart/add/product/1/",
         "rest": False, "group": "Add to cart"},
        {"name": "Set payment", "method": "POST",
         "path": "/uk/rest/uk/V1/carts/mine/payment-information",
         "rest": True, "group": "Checkout"},
    ],
}
BASE_PLAN = {"users": 10, "duration_s": 600, "duration_human": "10m", "spawn_rate": 1,
             "label": "Load Test", "think_time": [2, 5]}


def script(plan_extra=None):
    plan = dict(BASE_PLAN, **(plan_extra or {}))
    return _assemble_flow_script(DISCOVERY, plan, DISCOVERY["flow"], 2, 5)


print("1. Ramp-up is excluded from the statistics")
eng = LocustEngine()
args = eng.single_cmd(dict(BASE_PLAN, test_type="load"), "https://shop.example.com")
check("--reset-stats is passed by default", "--reset-stats" in args, True)
check("it survives into the distributed master too",
      "--reset-stats" in eng.master_cmd(dict(BASE_PLAN), "https://x", 4), True)
optout = eng.single_cmd(dict(BASE_PLAN, measure_ramp_up=True), "https://x")
check("measure_ramp_up: true opts back out", "--reset-stats" in optout, False)
check("nothing else about the command changed",
      [a for a in args if a != "--reset-stats"],
      eng.single_cmd(dict(BASE_PLAN, measure_ramp_up=True), "https://shop.example.com"))

print("\n2. Business transactions are timed end to end")
sc = script()
try:
    ast.parse(sc)
    check("the generated script still parses", True, True)
except SyntaxError as e:
    check("the generated script parses (line %s: %s)" % (e.lineno, e.msg), False, True)
check("a transaction timer exists", "def _txn_begin(self, name):" in sc, True)
check("and is closed by a matching end", "def _txn_end(self, failed_reason=None):" in sc, True)
check("it is per user instance, not module state",
      "self._txn_name" in sc and "\n_txn_name" not in sc, True)
check("a group change starts a new transaction",
      '_g != getattr(self, "_txn_name", None)' in sc, True)
check("samples are reported to Locust as their own entry",
      'request_type="TXN"' in sc and 'name="TXN: %s" % name' in sc, True)
check("an unplaced order marks the transaction failed",
      '"order not placed"' in sc, True)
# The validator can return early (a gate stops it), so its transaction must be
# closed in a finally. There is exactly ONE such wrapper: the recorded-order
# fallback is deliberately not wrapped, because the loop already timed its group.
check("the validator's transaction is closed in a finally",
      sc.count("finally:\n                self._txn_end(") == 1, True)
check("each iteration starts with no transaction open",
      "self._txn_name, self._txn_t0 = None, None" in sc, True)
check("the transaction is named from the recording, not hardcoded",
      "_CHECKOUT_TXN = next(" in sc, True)

# The recording's last group is what the end-to-end transaction is called.
m = re.search(r"_CHECKOUT_TXN = next\(\s*\(str\(s\.get\(\"group\"\)\) for s in reversed", sc)
check("it reads the last recorded group", bool(m), True)

print("\n3. Throughput can be held")
check("no pacing requested keeps think time",
      _wait_time_expr({"users": 10}, 2, 5), "between(2, 5)")
check("a target rate becomes throughput pacing, divided per user",
      _wait_time_expr({"users": 10, "target_tps": 5}, 2, 5),
      "constant_throughput(0.5)")
check("a single user takes the whole rate",
      _wait_time_expr({"users": 1, "target_tps": 3}, 2, 5),
      "constant_throughput(3)")
check("an iteration interval becomes constant pacing",
      _wait_time_expr({"users": 4, "pacing_s": 30}, 2, 5), "constant_pacing(30)")
check("a rate wins over an interval when both are given",
      _wait_time_expr({"users": 2, "target_tps": 1, "pacing_s": 30}, 2, 5),
      "constant_throughput(0.5)")
check("zero and nonsense fall back to think time",
      [_wait_time_expr({"users": 2, "target_tps": 0}, 2, 5),
       _wait_time_expr({"users": 2, "pacing_s": "abc"}, 2, 5)],
      ["between(2, 5)", "between(2, 5)"])
check("the strategy reaches the script",
      "constant_throughput(0.5)" in script({"target_tps": 5}), True)
check("and the pacing helpers are imported there",
      "from locust import HttpUser, task, between, constant_pacing, "
      "constant_throughput, events" in sc, True)
check("a paced script still parses",
      (lambda t: (ast.parse(t), True)[1])(script({"target_tps": 5})), True)

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
