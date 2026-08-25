"""Regression guard: business-critical calls must never be dropped silently.

Run after any change to the call selector in apea/server.py,
apea/static/index.html, or the login-URL derivation in generator.py:

    ./.venv/bin/python tests_pricing_call_guard.py     # expect: FAILURES: 0

Two storefront calls are business critical, and both are `rest=False`, so a bulk
"Only REST + API" selection drops them while every metric still reads 200 OK.

1. POST /checkout/cart/add runs the pricing the store itself applies (Radwell:
   custom modules, contract price). The REST /carts/mine/items endpoint bypasses
   it and adds the line at 0, so the order captures shipping and tax only.

2. POST /customer/account/loginPost authenticates the STOREFRONT session. Drop
   it and the generator falls back to unprefixed login endpoints; on a
   multi-store Magento the session is then authenticated on the DEFAULT store
   while every /uk/ call runs as a guest. The cart-add returns 200 but lands in
   a guest quote, so the customer's REST quote stays empty.

Both were observed against mcstaging.radwell.eu on 2026-08-25. Defect 1 wrote
11 orders at 41.99 each with the product value missing. Defect 2 then produced a
run where the cart-add ran four times, returned 200 every time, and added
nothing -- caught only because the cart-value gate refused to place the order.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from apea.server import _PRICING_CRITICAL_RE, _call_sig   # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print("   %-64s %-6s %s" % (label[:64], "OK" if ok else "FAIL",
                                "" if ok else "(got %r want %r)" % (got, want)))
    if not ok:
        fails.append(label)


print("The pattern recognises a storefront cart-add")
for path in ("/uk/checkout/cart/add/uenc/aHR0cHM6Ly9t~~/product/69388720/",
             "/checkout/cart/add",
             "/checkout/cart/add/",
             "/de/checkout/cart/add?form_key=abc",
             "/cart/add/product/12/",
             "/uk/customer/account/loginPost/referer/aHR0~~/",
             "/customer/account/loginPost",
             "/customer/ajax/login"):
    check("MATCH %s" % path[:52], bool(_PRICING_CRITICAL_RE.search(path)), True)

print("\nand does not fire on calls that only look similar")
for path in ("/uk/rest/uk/V1/carts/mine/items",
             "/uk/checkout/cart/",
             "/uk/checkout/cart/addressupdate",
             "/uk/checkout/cart/delete/id/9/",
             "/uk/checkout/cart/updatePost/",
             "/uk/getitby/index/getitbycart/",
             "/static/frontend/Magento_Checkout/template/cart/add.html",
             "/uk/customer/section/load/",
             "/uk/customer/account/create/"):
    check("NO MATCH %s" % path[:49], bool(_PRICING_CRITICAL_RE.search(path)), False)

print("\nA REST-only selection is detected as dropping it")
flow = [
    {"method": "POST", "path": "/uk/checkout/cart/add/uenc/A~~/product/69388720/",
     "rest": False},
    {"method": "POST", "path": "/uk/rest/uk/V1/carts/mine/shipping-information",
     "rest": True},
    {"method": "GET", "path": "/uk/rest/uk/V1/carts/mine/totals", "rest": True},
    {"method": "POST", "path": "/uk/getitby/index/getitbycart/", "rest": False},
    {"method": "POST", "path": "/uk/customer/account/loginPost/referer/aHR0~~/",
     "rest": False},
]
rest_only = {_call_sig(s) for s in flow if s.get("rest")}
dropped = [_call_sig(s) for s in flow
           if _call_sig(s) not in rest_only
           and _PRICING_CRITICAL_RE.search(str(s.get("path") or ""))]
check("REST-only selection drops 2 critical calls", len(dropped), 2)
check("the cart-add is one",
      any("checkout/cart/add" in d for d in dropped), True)
check("the storefront login is the other",
      any("loginPost" in d for d in dropped), True)

print("\nThe corrected rule (REST + API + pricing-critical) keeps it")
fixed = {_call_sig(s) for s in flow
         if s.get("rest") or _PRICING_CRITICAL_RE.search(str(s.get("path") or ""))}
still_dropped = [_call_sig(s) for s in flow
                 if _call_sig(s) not in fixed
                 and _PRICING_CRITICAL_RE.search(str(s.get("path") or ""))]
check("no pricing call is dropped", len(still_dropped), 0)
check("selection grew by both", len(fixed) - len(rest_only), 2)

print("\nThe browser UI carries the same rule")
html = (ROOT / "apea" / "static" / "index.html").read_text(encoding="utf-8")
check("index.html defines PRICING_CRITICAL_RE", "PRICING_CRITICAL_RE" in html, True)
check('"Only REST + API" honours data-critical',
      "el.dataset.critical==='1'" in html, True)
check("rows carry data-critical", 'data-critical="${crit?' in html, True)
check("a warning element exists", 'id="callCritWarn"' in html, True)

js = re.search(r"const PRICING_CRITICAL_RE = /(.+?)/i;", html)
check("the JS and Python patterns are the same rule",
      (js.group(1).replace("\\/", "/") if js else None),
      _PRICING_CRITICAL_RE.pattern)

print("\nThe generator prefixes its fallback login URLs with the store code")
gen = (ROOT / "apea" / "agents" / "generator.py").read_text(encoding="utf-8")
check("generator derives a store prefix", "_store_pfx" in gen, True)
check("and prepends it to the login fallbacks",
      "_store_pfx + _g for _g in _fallbacks" in gen, True)
check("keeping the unprefixed ones as a fallback",
      "+ _fallbacks" in gen, True)

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
