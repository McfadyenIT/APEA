"""Regression guard: business-critical calls must never be dropped silently,
on ANY commerce platform.

Run after any change to the call selector in ltmetrics/server.py,
ltmetrics/static/index.html, the login-URL derivation in generator.py, or the
business_critical_paths blocks in knowledge/rules/platform_rules.yaml:

    ./.venv/bin/python tests_pricing_call_guard.py     # expect: FAILURES: 0

Two storefront calls are business critical, and both are `rest=False`, so a bulk
"Only REST + API" selection drops them while every metric still reads 200 OK.

1. The cart-add controller runs the pricing the store itself applies (custom
   modules, contract/tier price). An API add bypasses it and can land the line
   at 0, so the order captures shipping and tax only.

2. The storefront login authenticates the session. Drop it and the generator
   falls back to unprefixed login endpoints; on a multi-store install the
   session is then authenticated on the DEFAULT store while every store-scoped
   call runs as a guest. The cart-add returns 200 and adds to a guest cart.

Both were observed against a live Magento store on 2026-08-25. Defect 1 wrote 11
orders at 41.99 each with the product value missing. Defect 2 then produced a run
where the cart-add ran four times, returned 200 every time, and added nothing --
caught only because the cart-value gate refused to place the order.

LT Metrics is not a single-store tool, so the paths are NOT hardcoded: they live in
platform_rules.yaml per platform, with a deliberately broad `generic` block as
the floor. A false positive costs one extra call in the script; a false negative
costs a whole run of zero-value orders that reports success.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from ltmetrics.server import _call_sig, _critical_kind, _critical_paths   # noqa: E402
from ltmetrics.knowledge import KB   # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print("   %-64s %-6s %s" % (label[:64], "OK" if ok else "FAIL",
                                "" if ok else "(got %r want %r)" % (got, want)))
    if not ok:
        fails.append(label)


print("The rule lives in the knowledge base, not in code")
check("platform_rules.yaml defines a generic floor",
      bool((KB.platform_rules("generic") or {}).get("business_critical_paths")), True)
check("magento declares its own paths",
      bool((KB.platform_rules("magento") or {}).get("business_critical_paths")), True)
_named = [p for p in KB.platforms()
          if (KB.platform_rules(p) or {}).get("business_critical_paths")]
check("more than one platform is taught (not Magento-only)", len(_named) >= 3, True)
check("the generic floor is merged into every platform",
      set(_critical_paths("generic")["cart_add"])
      <= set(_critical_paths("magento")["cart_add"]), True)

print("\nIt recognises a cart-add across platforms")
for path, plat in (("/uk/checkout/cart/add/uenc/aHR0~~/product/69388720/", "magento"),
                   ("/checkout/cart/add", "magento"),
                   ("/cart/add.js", "shopify"),
                   ("/cart/add", "shopify"),
                   ("/en/cart.php?action=add&product_id=9", "bigcommerce"),
                   ("/rest/v2/store/users/x/carts/y/entries", "sap_commerce"),
                   ("/store/addToCart", "generic"),
                   ("/p/index.php?add-to-cart=42", "generic")):
    check("cart_add  %-46s (%s)" % (path[:46], plat),
          _critical_kind(path, plat), "cart_add")

print("\nand a storefront login across platforms")
for path, plat in (("/uk/customer/account/loginPost/referer/aHR0~~/", "magento"),
                   ("/customer/ajax/login", "magento"),
                   ("/account/login", "shopify"),
                   ("/login.php", "bigcommerce"),
                   ("/j_spring_security_check", "sap_commerce"),
                   ("/auth/signin", "generic")):
    check("login     %-46s (%s)" % (path[:46], plat),
          _critical_kind(path, plat), "login")

print("\nand stays quiet on calls that only look similar")
for path in ("/uk/rest/uk/V1/carts/mine/items",
             "/uk/checkout/cart/",
             "/uk/checkout/cart/delete/id/9/",
             "/uk/checkout/cart/updatePost/",
             "/uk/getitby/index/getitbycart/",
             "/static/frontend/Magento_Checkout/template/cart/add.html",
             "/uk/customer/section/load/",
             "/uk/customer/account/create/"):
    check("ordinary  %s" % path[:52], _critical_kind(path, "magento"), "")

print("\nA REST-only selection is detected as dropping both")
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
dropped = [(_call_sig(s), _critical_kind(s["path"], "magento")) for s in flow
           if _call_sig(s) not in rest_only and _critical_kind(s["path"], "magento")]
check("REST-only drops 2 critical calls", len(dropped), 2)
check("one is the cart-add", sorted(k for _s, k in dropped)[0], "cart_add")
check("the other is the login", sorted(k for _s, k in dropped)[1], "login")

print("\nThe corrected rule keeps both")
fixed = {_call_sig(s) for s in flow
         if s.get("rest") or _critical_kind(s["path"], "magento")}
still = [s for s in flow
         if _call_sig(s) not in fixed and _critical_kind(s["path"], "magento")]
check("nothing critical is dropped", len(still), 0)
check("the selection grew by exactly two", len(fixed) - len(rest_only), 2)

print("\nThe browser keeps NO second copy of the rule")
html = (ROOT / "ltmetrics" / "static" / "index.html").read_text(encoding="utf-8")
check("no regex in the page", "PRICING_CRITICAL_RE" not in html, True)
check("it reads the server's flag instead", "c && c.critical" in html, True)
check("rows carry the KIND, not a boolean",
      "data-critical=\"${_escAttr(c.critical||'')}\"" in html, True)
check("bulk select honours it", "!!el.dataset.critical" in html, True)
check("the warning explains the actual kind", "CRITICAL_WHY[kind]" in html, True)

srv = (ROOT / "ltmetrics" / "server.py").read_text(encoding="utf-8")
check("the server computes it from the KB", "KB.platform_rules(src)" in srv, True)
check("and detects the platform per recording", "_platform_of(rec, flow)" in srv, True)

print("\nThe generator prefixes its fallback login URLs with the store code")
gen = (ROOT / "ltmetrics" / "agents" / "generator.py").read_text(encoding="utf-8")
check("generator derives a store prefix", "_store_pfx" in gen, True)
check("and prepends it to the login fallbacks",
      "_store_pfx + _g for _g in _fallbacks" in gen, True)
check("keeping the unprefixed ones as a fallback", "+ _fallbacks" in gen, True)

print()

# --- appended: the substitute must be PRICED, not merely in stock -------------
print()
print("the out-of-stock fallback will not pick a 0-priced product")
import io as _io2
_src = _io2.open("ltmetrics/agents/generator.py", encoding="utf-8").read()
_blk = _src.split("LAST-RESORT in-stock fallback")[1][:2600]
for label, needle in (
        ("a price is read from the candidate", 'float(_fc.get("price")'),
        ("final_price is accepted too", '_fc.get("final_price")'),
        ("a non-positive price is skipped", "if _fprice <= 0:"),
        ("and the skip is logged, not silent", "skipped: in stock but"),
):
    ok = needle in _blk
    print("   %-52s %s" % (label, "OK" if ok else "FAIL"))
    if not ok:
        fails.append(label)

print()
print("a 0-price stop says whether the sku was SUBSTITUTED")
for label, needle in (
        ("the data's own sku is remembered", "_orig_sku = str((self._row or {})"),
        ("the message compares them", "sku != _orig_sku"),
        ("and names what to do", "run in strict mode to fail on the real one"),
):
    ok = needle in _src
    print("   %-52s %s" % (label, "OK" if ok else "FAIL"))
    if not ok:
        fails.append(label)
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
