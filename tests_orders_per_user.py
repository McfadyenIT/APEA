"""Stopping each user after N orders.

A smoke test wants a KNOWN number of orders -- one per user -- and a duration
cannot express that. It loops until the clock runs out, so the count depends on
how fast the store happened to be that minute: the same two-minute run produced
16 orders on a good pass and 2 on a slow one. Locust in this build has no
--iterations, so the cap lives in the generated script.

Run:  ./.venv/bin/python tests_orders_per_user.py     # expect FAILURES: 0
"""
import io
import re
import sys

sys.path.insert(0, ".")

from ltmetrics.agents import generator as G  # noqa: E402

SRC = io.open(G.__file__, encoding="utf-8").read()
SRV = io.open("ltmetrics/server.py", encoding="utf-8").read()
UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()
FAILURES = []


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name[:58], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


print("the cap reaches the script")
check("the script has the setting", "_MAX_ORDERS_PER_USER = __MAX_ORDERS_PER_USER__" in SRC)
check("it is substituted at generation time",
      '"__MAX_ORDERS_PER_USER__": repr(int(' in SRC)
check("from the plan's orders_per_user", 'get("orders_per_user")' in SRC)
check("0 means no cap, which is the old behaviour",
      "or 0" in SRC.split("__MAX_ORDERS_PER_USER__")[2][:120])

print()
print("it stops BEFORE starting another order, not after")
body = SRC.split("def checkout(self):")[1][:900]
check("the check is the first thing in the task",
      body.index("_MAX_ORDERS_PER_USER") < body.index("self._order_placed = False"))
check("it raises StopUser", "raise StopUser()" in body)
check("and says why in the call log", "placed its %d order(s)" in body)
check("the reason is recorded", "should not add another basket" in body)

print()
print("only a PLACED order counts")
check("the counter increments on success only",
      "if self._order_placed:" in SRC
      and "self._orders_done = getattr(self, \"_orders_done\", 0) + 1" in SRC)

print()
print("the operator can set it")
check("the server accepts it", "orders_per_user" in SRV)
check("it reaches the plan", '"orders_per_user": int(req.orders_per_user or 0)' in SRV)
check("the page has a field", 'id="ovOrders"' in UI)
check("the page sends it", "body.orders_per_user" in UI)
check("and explains what a duration cannot do",
      "loops" in UI and "clock runs out" in UI)

print()
print("a generated script with a cap of 1 compiles")
i = SRC.index("tmpl = r'''")
start = SRC.index("'''", i) + 3
t = SRC[start:SRC.index("'''", start)]
t = t.replace("__MAX_ORDERS_PER_USER__", "1")
t = t.replace("__FORCED_PAYMENT__", "'{{payment_method}}'")
t = t.replace("__PAYMENT_ADDL__", "{'card_id': '{{payment_token}}'}")
t = t.replace("__HOSTED_GATEWAYS__", "['cybersource']")
t = t.replace("__WAIT_TIME__", "between(1, 2)")
t = "\n".join(l for l in t.split("\n") if not re.fullmatch(r"__[A-Z0-9_]+__", l.strip()))
t = re.sub(r"__[A-Z0-9_]+__", "0", t)
try:
    compile(t, "generated", "exec")
    check("it compiles", True)
except SyntaxError as exc:
    check("it compiles", False, "line %s: %s" % (exc.lineno, exc.msg))
check("the cap is baked in as 1", "_MAX_ORDERS_PER_USER = 1" in t)


# --------------------------------------------------------------------------
# The value has to REACH the template.
#
# Everything above passed while the feature was completely dead: the operator
# set "1 order per user" and got 8 orders. The number was wired to the plan
# PREVIEW only. RunReq had no such field, the run never put it into plan_cfg,
# and the page never sent it when generating -- so the template substituted 0
# every time and the cap could not fire.
#
# Testing the cap without testing the wiring is how that shipped. Trace the
# whole path here: page -> request model -> plan -> template.
# --------------------------------------------------------------------------
print()
print("the number the operator types reaches the generated script")

# 1. the page sends it when it GENERATES, not only when it previews
_gen = UI.split("btnRun")[1] if "btnRun" in UI else UI
check("the generate request carries it",
      UI.count("body.orders_per_user=+$('#ovOrders').value") >= 2)
_preview = UI[UI.index("async function refreshPlan("):][:1600]
check("and the preview still does too", "orders_per_user" in _preview)

# 2. the run request can hold it
_runreq = SRV.split("class RunReq")[1].split("\nclass ")[0]
check("RunReq accepts it", "orders_per_user" in _runreq)

# 3. the run puts it in the plan the generator reads
check("the run writes it into plan_cfg",
      'plan_cfg["orders_per_user"] = int(req.orders_per_user or 0)' in SRV)

# 4. the generator reads that key
check("the generator reads that same key",
      '(plan_cfg or {}).get("orders_per_user")' in SRV or
      '(plan_cfg or {}).get("orders_per_user")' in SRC)

# 5. and it is not left behind in the preview-only model
check("PlanReq still has it for the estimate",
      "orders_per_user" in SRV.split("class PlanReq")[1].split("\nclass ")[0])

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
