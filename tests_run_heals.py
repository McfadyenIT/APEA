"""Regression guard: three failures that were the tool's, not the store's.

Run after any change to _heal, _rest_place_order or the catalogue resolve in
ltmetrics/agents/generator.py:

    ./.venv/bin/python tests_run_heals.py     # expect: FAILURES: 0

Each one was observed on a live store on 2026-09-04, and each put a defect in a
client's report that the client did not have.

1. STALE STOREFRONT CART  (Amneal eb762c912965 — 2 of 3 failures)
   The tool places the order through the API. That consumes the quote, but the
   browser session still holds its id, so the NEXT iteration's storefront
   cart-add resolves a dead one and comes back 200 with
   {"error":true,"error_messages":["No such entity with cartId = 16195"]}.
   Magento clears the stale id when that lookup throws, so a retry lands on a
   fresh quote — the failed attempt is itself the reset. _heal had no rule for
   it, so nothing retried. Generic: every store that places by API and prices by
   storefront cart-add hits it from its second iteration onwards.

2. ORDER FALLBACK WITH NO SHIPPING REMEDY  (Radwell 4733cf79e407 — 1 failure)
   A Magento quote can drop its shipping-address assignment between the shipping
   call and the order ("The shipping address is missing. Set the address and try
   again."). The recorded path has healed that for weeks. The REST fallback,
   which runs exactly when the main path came up short, never did — so the
   transient ended the run there instead.

3. AN EMPTY SEARCH IS NOT A SITE FAILURE  (Amneal — 1 failure)
   The data said "429", which is a product ID from the recorded URL, not a sku.
   GraphQL answered 200 with zero matches, which is correct, and the tool
   recorded a failed request against the store. Our data problem, printed in
   their report as their defect.

The Locust script is a template string inside generator.py, so it is extracted
and parsed here rather than imported.
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


def _template():
    """The generated Locust script, with block placeholders neutralised."""
    module = ast.parse(io.open(SRC, encoding="utf-8").read())
    tpl = next((n.value for n in ast.walk(module)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "def checkout" in n.value), None)
    if tpl is None:
        return None, None
    ph = re.compile(r"^\s*__[A-Z0-9_]+__\s*$")
    lines = tpl.splitlines()
    out = []
    for i, ln in enumerate(lines):
        if ph.match(ln):
            nxt = next((l for l in lines[i + 1:] if l.strip()), "")
            out.append(" " * (len(nxt) - len(nxt.lstrip())) + "pass")
        else:
            out.append(ln)
    return tpl, ast.parse("\n".join(out))


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _src(tpl, node):
    return "\n".join(tpl.splitlines()[node.lineno - 1:node.end_lineno])


print("\n== the tool stops reporting its own failures as the store's ==")

tpl, tree = _template()
check("the Locust script template parses", tree is not None)

heal = _func(tree, "_heal") if tree else None
place = _func(tree, "_rest_place_order") if tree else None
resolve = _func(tree, "_graphql_search") if tree else None

print("\n1. a storefront cart the last order consumed")
check("_heal exists", heal is not None)
if heal is not None:
    h = _src(tpl, heal)
    check("it recognises a cart id the store no longer has",
          "no such entity with cart" in h)
    idx = h.find("no such entity with cart")
    tail = h[idx:]
    check("a retry is still possible when the add really did miss",
          "return True" in tail)
    check("the operator is told why the retry happened",
          "_clog_annotate" in tail)
    # The retry must never be blind. "No such entity with cartId" can come back
    # from a request that ALREADY landed the item: the store clears the dead
    # quote id, creates a fresh one, adds, and only then throws from a custom
    # cart module. Retrying that bought a second unit of a product limited to
    # one -- four of six Amneal orders came out at 94.96 instead of 47.48, an
    # order the store would never have taken, reported as a success.
    check("it reads the cart before retrying, rather than retrying blind",
          "AUTO-HEAL check cart" in tail)
    check("an item already in the quote cancels the retry",
          "return False" in tail and tail.index("AUTO-HEAL check cart") < tail.index("return True"))
    check("and a cart it cannot read also cancels it",
          "not be read" in tail)
    check("a different missing entity is not swallowed",
          "no such entity with cart" in h and "no such entity\"" not in h)

print("\n2. the order fallback heals a dropped shipping address")
check("_rest_place_order exists", place is not None)
if place is not None:
    p = _src(tpl, place)
    check("it can be told it has already healed once",
          any(a.arg == "_healed" for a in place.args.args))
    check("it asks for the same remedy the recorded path uses",
          "self._heal(" in p)
    check("the retry is counted as a heal, not hidden", '_bump("heals")' in p)
    check("and it can only retry once",
          "_rest_place_order(_healed=True)" in p
          and p.count("self._rest_place_order(") == 1)

print("\n3. a correct 'no match' is not charged to the store")
check("the catalogue resolve exists", resolve is not None)
if resolve is not None:
    q = _src(tpl, resolve)
    check("an HTTP error is still a failure",
          'r.failure("graphql resolve status=%s" % r.status_code)' in q)
    check("a served request with no matches is a success", "r.success()" in q)
    check("but it is not silent — the operator is told it is the data",
          "_clog_annotate" in q and "not a" in q)
    check("and it is counted so a run can be judged on it",
          '_bump("resolve_empty")' in q)
    # The old behaviour: any empty result failed the request outright.
    check("the old blanket failure is gone",
          "items=%d" % 0 not in q and 'items=%d" % (r.status_code' not in q)

print("\nFAILURES: %d" % len(fails))
for f in fails:
    print("  - %s" % f)
sys.exit(1 if fails else 0)
