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

from apea.agents import generator as G  # noqa: E402

SRC = io.open(G.__file__, encoding="utf-8").read()
SRV = io.open("apea/server.py", encoding="utf-8").read()
UI = io.open("apea/static/index.html", encoding="utf-8").read()
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

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
