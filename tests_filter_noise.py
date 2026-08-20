"""Regression guard for the recording noise filter.

Run after any change to apea/agents/filter.py or apea/agents/parser.py:

    ./.venv/bin/python tests_filter_noise.py      # expect: FAILURES: 0

Covers six defects found by testing the filter against the real Radwell
recording plus synthetic HAR/JMX fixtures.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apea.agents import filter as f
from apea.agents import parser as P

fails = []


def check(label, got, want):
    ok = got == want
    print("   %-64s %-6s %s" % (label[:64], "OK" if ok else "FAIL",
                                "" if ok else "(got %r want %r)" % (got, want)))
    if not ok:
        fails.append(label)


print("FIX 1 - .html is a dynamic page, not a static asset")
check("is_asset('/uk/buy/x/24071467.html')", f.is_asset("/uk/buy/x/24071467.html"), False)
check("product page reason mentions page navigation",
      "page navigation" in f.static_reason("GET", "/uk/buy/x/24071467.html"), True)
check("real assets are still assets", f.is_asset("/uk/theme/styles.css"), True)

print("\nFIX 2 - trackers are dropped")
for p in ("/collect?v=1&tid=UA-1", "/gtm/collect", "/beacon?id=9",
          "https://www.google-analytics.com/collect", "/gtag/js?id=G-1",
          "/pixel?x=1", "/js/newrelic.js"):
    check("DROP %s" % p, f.is_static_request("GET", p), True)
check("tracker reason", f.static_reason("GET", "/collect?v=1"),
      "third-party tracker / analytics beacon")

print("\nFIX 2b - no false positives on real paths")
for p in ("/uk/collections/shoes", "/uk/collect-in-store/info",
          "/uk/rest/uk/V1/carts/mine/collectpoints?x=1", "/uk/pixelated-art.html"):
    check("not a tracker: %s" % p, f.is_tracker(p), False)

print("\nFIX 3 - CORS preflight never survives")
check("OPTIONS dropped", f.is_static_request("OPTIONS", "/uk/rest/uk/V1/carts/mine"), True)
check("preflight reason", "CORS preflight" in f.static_reason("OPTIONS", "/x"), True)

print("\nFIX 4 - third-party hosts dropped in HAR and JMX")
har = {"log": {"entries": [
    {"request": {"method": "GET", "url": "https://shop.example.com/uk/rest/V1/x"}},
    {"request": {"method": "POST", "url": "https://shop.example.com/uk/checkout/cart/add/"}},
    {"request": {"method": "GET", "url": "https://shop.example.com/uk/graphql?q=1"}},
    {"request": {"method": "GET", "url": "https://www.google-analytics.com/collect?v=1"}},
    {"request": {"method": "GET", "url": "https://cdn.other.net/lib/app.js"}},
    {"request": {"method": "OPTIONS", "url": "https://shop.example.com/uk/rest/V1/x"}},
]}}
d = Path(tempfile.mkdtemp())
hp = d / "t.har"
hp.write_text(json.dumps(har), encoding="utf-8")
res = P._parse_har(hp)
print("     kept:", [e["path"] for e in res["endpoints"]])
check("HAR base_url is the busiest host", res["base_url"], "https://shop.example.com")
check("HAR kept exactly the 3 real calls", len(res["endpoints"]), 3)
check("HAR dropped the analytics host",
      any("third-party host" in x["reason"] for x in res["dropped"]), True)
check("HAR dropped the preflight",
      any("CORS preflight" in x["reason"] for x in res["dropped"]), True)

JMX = """<jmeterTestPlan><hashTree>
 <HTTPSamplerProxy testname="rest"><stringProp name="HTTPSampler.method">GET</stringProp>
  <stringProp name="HTTPSampler.path">/uk/rest/V1/x</stringProp>
  <stringProp name="HTTPSampler.domain">shop.example.com</stringProp></HTTPSamplerProxy>
 <HTTPSamplerProxy testname="add"><stringProp name="HTTPSampler.method">POST</stringProp>
  <stringProp name="HTTPSampler.path">/uk/checkout/cart/add/</stringProp>
  <stringProp name="HTTPSampler.domain">shop.example.com</stringProp></HTTPSamplerProxy>
 <HTTPSamplerProxy testname="ga"><stringProp name="HTTPSampler.method">GET</stringProp>
  <stringProp name="HTTPSampler.path">/collect?v=1</stringProp>
  <stringProp name="HTTPSampler.domain">www.google-analytics.com</stringProp></HTTPSamplerProxy>
 <HTTPSamplerProxy testname="pre"><stringProp name="HTTPSampler.method">OPTIONS</stringProp>
  <stringProp name="HTTPSampler.path">/uk/rest/V1/x</stringProp>
  <stringProp name="HTTPSampler.domain">shop.example.com</stringProp></HTTPSamplerProxy>
</hashTree></jmeterTestPlan>"""
jp = d / "t.jmx"
jp.write_text(JMX, encoding="utf-8")
rj = P._parse_jmx(jp)
print("     kept:", [e["path"] for e in rj["endpoints"]])
check("JMX kept exactly the 2 real calls", len(rj["endpoints"]), 2)
check("JMX dropped the analytics host",
      any("third-party host" in x["reason"] for x in rj["dropped"]), True)
check("JMX dropped the preflight",
      any("CORS preflight" in x["reason"] for x in rj["dropped"]), True)

print("\nFIX 5 - reasons are distinct and accurate")
for path, want in (("/static/v1/a.css", "static file served by the web server"),
                   ("/uk/theme/custom.css", "static asset"),
                   ("/uk/buy/x.html", "page navigation"),
                   ("/collect?v=1", "third-party tracker")):
    check("reason(%s)" % path, want in f.static_reason("GET", path), True)

print("\nFIX 6 - files under a framework static mount are never load-tested")
for p in ("/static/version1779429086/frontend/Magento/base/default/"
          "Magento_Checkout/template/payment-methods/list.html",
          "/static/version1779429086/frontend/McFadyen/LumaCheckout/default/"
          "Magento_Checkout/template/summary.html",
          "/pub/static/frontend/a/b.js", "/media/catalog/product/x.jpg",
          "/assets/app.abc123.css", "/_next/static/chunks/main.js",
          "/wp-content/uploads/photo.png", "/uk/static/v1/tpl.html"):
    check("DROP (static mount) ...%s" % p[-44:], f.is_static_request("GET", p, xhr=True), True)
check("static-mount reason", f.static_reason("GET", "/static/v1/x/tpl.html"),
      "static file served by the web server / CDN (not the application)")

print("\nFIX 6b - real endpoints under those prefixes are NOT caught")
for m, p, xhr in (("POST", "/media/upload", False),
                  ("GET", "/api/media/library?page=1", False),
                  ("POST", "/uk/staticpages/save", False)):
    check("KEEP %-6s %s" % (m, p), f.is_static_request(m, p, xhr), False)

print("\nUNCHANGED - the calls a load test needs are still kept")
for m, p in (("POST", "/uk/checkout/cart/add/uenc/A~~/product/69388720/"),
             ("POST", "/uk/getitby/index/getitbyproductdetail"),
             ("POST", "/uk/pdpdataprovider/index/index/"),
             ("GET", "/uk/rest/uk/V1/carts/mine/totals"),
             ("GET", "/uk/catalogsearch/result/?q=124436215"),
             ("POST", "/uk/customer/account/loginPost/referer/aHR0~~/")):
    check("KEEP %-6s %s" % (m, p[:48]), f.is_static_request(m, p), False)

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
