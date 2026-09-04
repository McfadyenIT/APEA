"""Regression guard: a checkout the fallback rescued is a checkout that PASSED.

Run after any change to `checkout()` in ltmetrics/agents/generator.py:

    ./.venv/bin/python tests_checkout_txn_outcome.py     # expect: FAILURES: 0

The order can be placed by two mechanisms: the API validator (`_rest_checkout`,
reported as "Order created") and, when that comes up short, the recorded order
step (`_place_order`, reported as "Place Order (REST)" / "(replay)"). Both end
with a real order id from the store.

The checkout transaction used to be closed BEFORE the fallback ran. So when the
validator missed and the fallback succeeded a moment later, the run reported

    TXN: Checkout   1 failure   Exception('order not placed')

for an order the store had actually accepted -- and, because the per-user
counter sat in the same closed branch, that order never counted towards
`orders_per_user`, so a run capped at one order could place several.

Observed on a live Magento store on 2026-09-04 (run e0872e29b59f): three
checkouts, three order ids (63091, 63094, 63097), every order call reporting 0
failures -- and one TXN: Checkout failure. That 1.449% error rate was the
tool's, not the store's.

The invariants below are structural rather than textual: the fallback has to sit
inside the try whose finally closes the transaction, and the per-user counter
has to come after it. The Locust script is a template string inside
generator.py, so it is extracted and parsed here rather than imported.
"""
import ast
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "ltmetrics" / "agents" / "generator.py"

fails = []


def check(label, ok):
    print("   %-64s %s" % (label[:64], "OK" if ok else "FAIL"))
    if not ok:
        fails.append(label)


def _script_ast():
    """Parse the generated Locust script out of the template that produces it.

    Block placeholders (`__BROWSE_TASKS__`) sit at column 0 and are replaced at
    build time with an indented block, so the raw template does not parse. Each
    becomes a `pass` at the indentation of the line that follows it.
    """
    module = ast.parse(io.open(SRC, encoding="utf-8").read())
    tpl = next((n.value for n in ast.walk(module)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "def checkout" in n.value), None)
    if tpl is None:
        return None
    ph = re.compile(r"^\s*__[A-Z0-9_]+__\s*$")
    lines = tpl.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if ph.match(ln):
            nxt = next((l for l in lines[i + 1:] if l.strip()), "")
            out.append(" " * (len(nxt) - len(nxt.lstrip())) + "pass")
        else:
            out.append(ln)
    return ast.parse("\n".join(out))


def _method_calls(nodes, name):
    """Every `self.<name>(...)` call under `nodes` (a node or a list of them)."""
    if not isinstance(nodes, list):
        nodes = [nodes]
    out = []
    for n in nodes:
        for sub in ast.walk(n):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == name):
                out.append(sub)
    return out


print("\n== a rescued checkout is reported as a success ==")

tree = _script_ast()
check("the Locust script template parses", tree is not None)

funcs = {}
for node in ast.walk(tree or ast.parse("")):
    if isinstance(node, ast.FunctionDef):
        funcs.setdefault(node.name, node)

checkout = funcs.get("checkout")
check("checkout() exists", checkout is not None)
check("the fallback is its own method, so both paths share it",
      "_order_fallback" in funcs)

if checkout is not None:
    # The one try/finally that closes the checkout transaction.
    txn_tries = [n for n in ast.walk(checkout)
                 if isinstance(n, ast.Try) and _method_calls(n.finalbody, "_txn_end")]
    check("exactly one try/finally closes the checkout transaction",
          len(txn_tries) == 1)

    if len(txn_tries) == 1:
        t = txn_tries[0]
        check("the validator runs inside it",
              bool(_method_calls(t.body, "_rest_checkout")))
        inner = _method_calls(t.body, "_order_fallback")
        check("the fallback runs inside it too, so its order counts as a pass",
              bool(inner))

        # The per-user counter must not be decided before the fallback has had
        # its turn -- that is exactly the bug this file guards.
        counters = [n for n in ast.walk(checkout)
                    if isinstance(n, ast.Assign)
                    and any(isinstance(tg, ast.Attribute) and tg.attr == "_orders_done"
                            for tg in n.targets)]
        check("the per-user order counter is set in one place", len(counters) == 1)
        check("it is counted after the fallback, not before",
              bool(counters) and bool(inner)
              and counters[0].lineno > min(c.lineno for c in inner))

# The guard that stops a pointless order attempt has to survive the move.
fb = funcs.get("_order_fallback")
if fb is not None:
    src = ast.dump(fb)
    check("an empty cart still skips the attempt rather than cascading",
          "item_count" in src and "stopped_at" in src)
    check("the fallback still places through _place_order",
          bool(_method_calls(fb, "_place_order")))

print("\nFAILURES: %d" % len(fails))
for f in fails:
    print("  - %s" % f)
sys.exit(1 if fails else 0)
