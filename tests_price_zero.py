"""Regression guard: a price of 0 the storefront corrected is not a finding.

Run after any change to _api_item_price in ltmetrics/agents/business_data.py or
_price_heal in ltmetrics/agents/executor.py:

    ./.venv/bin/python tests_price_zero.py     # expect: FAILURES: 0

Radwell run dd51824f28cb placed order 63118 at 6363.36 with the line priced at
5267.81 -- correctly. The report still led with two P1s saying the API had added
it at 0.0 and that the real price had to be scraped from the storefront. Both
were false for that run, and both came from the same mistake: treating ANY price
of 0 anywhere in the run as the price the order used.

On a client-side-priced catalogue the REST add ALWAYS lands at 0 first. That is
why the storefront cart-add is replayed afterwards and why the cart-value gate
exists. So a 0 appears in nearly every Radwell run, and whether it became a P1
depended only on which /items response the scan reached first:

    dd51824f28cb   prices seen [0.0, 5267.81, 5267.81]   -> fired (wrongly)
    9c4dbb2044be   prices seen [5267.81, 5267.81]        -> did not fire

Same store, same data file, same settings, opposite verdict. That is why it
survived a morning of testing and then appeared during a demo.

The rule now: the API price is the one the cart held when the order was placed,
and the heal fires only for a cart that NEVER priced -- which is also the case
the cart-value gate already stops before an order is written.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ltmetrics.agents.business_data import _api_item_price   # noqa: E402

fails = []


def check(label, ok):
    print("   %-64s %s" % (label[:64], "OK" if ok else "FAIL"))
    if not ok:
        fails.append(label)


def flow(*prices):
    """A timeline of /items responses carrying the given line prices."""
    return {"timeline": [
        {"url": "https://x/rest/V1/carts/mine/items",
         "body": '[{"item_id":1,"sku":"S","price":%s}]' % p, "ok": True}
        for p in prices]}


def heal_fires(f):
    """The condition _price_heal now uses, kept in step with executor.py."""
    seen = []
    for s in f.get("timeline") or []:
        if "items" not in str(s.get("url") or "").lower():
            continue
        for m in re.finditer(r'"price"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
                             str(s.get("body") or "")):
            seen.append(float(m.group(1)))
    return bool(seen) and not any(v > 0 for v in seen)


print("\n== a transient 0 is not the price the order used ==")

print("\nthe REST add lands at 0, the storefront then prices it")
f = flow(0, 5267.81, 5267.81)          # exactly Radwell dd51824f28cb
check("the API price is what the cart held, not what it passed through",
      _api_item_price(f) == 5267.81)
check("and no price heal is raised", not heal_fires(f))

print("\nthe same run without the leading 0 must agree")
f = flow(5267.81, 5267.81)             # exactly Radwell 9c4dbb2044be
check("same answer", _api_item_price(f) == 5267.81)
check("still no heal", not heal_fires(f))

print("\na cart that never priced is a real finding and must still fire")
f = flow(0, 0, 0)                      # exactly Amneal 80f798579667
check("the API price is 0", _api_item_price(f) == 0.0)
check("and the heal IS raised", heal_fires(f))

print("\nordering must not change the verdict")
check("priced-then-zero reads the same as zero-then-priced",
      _api_item_price(flow(89.0, 0)) == 89.0
      and _api_item_price(flow(0, 89.0)) == 89.0)
check("neither ordering raises a heal",
      not heal_fires(flow(89.0, 0)) and not heal_fires(flow(0, 89.0)))

print("\nedge cases")
check("no /items responses at all -> no price, no heal",
      _api_item_price({"timeline": []}) is None and not heal_fires({"timeline": []}))
check("a response with no price at all is ignored",
      _api_item_price({"timeline": [
          {"url": "https://x/carts/mine/items", "body": "[]", "ok": True}]}) is None)
check("prices outside an /items response are not read",
      _api_item_price({"timeline": [
          {"url": "https://x/catalog/product", "body": '{"price":1.0}'}]}) is None)

print("\nFAILURES: %d" % len(fails))
for f_ in fails:
    print("  - %s" % f_)
sys.exit(1 if fails else 0)
