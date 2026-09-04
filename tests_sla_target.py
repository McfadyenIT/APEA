"""Recommendations judge a run against ITS OWN SLA.

A passing Smoke Test carried "p95 4100ms over the 3000ms target" while the KPI
card beside it showed the same 4100 ms in green against 5000 ms. One report,
one number, two verdicts -- because the recommender used a module constant
(the Load Test figure) instead of the SLA the run was planned with.

These call the real builder rather than matching source text, so a future
regression that reintroduces the constant fails here.

Run:  ./.venv/bin/python tests_sla_target.py     # expect FAILURES: 0
"""
import io
import sys

sys.path.insert(0, ".")

from ltmetrics.agents import recommendation  # noqa: E402
from ltmetrics.config import DEFAULT_P95_THRESHOLD_MS  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


def build(p95, err, sla):
    """Recommendation titles for one run."""
    analysis = {"overall": {"p95": p95, "error_rate": err},
                "endpoints": [], "sla": sla}
    return [r.get("title", "") for r in recommendation.build(analysis, {}, None)]


SMOKE = {"max_p95_ms": 5000, "max_error_rate_pct": 1.0}
LOAD = {"max_p95_ms": 3000, "max_error_rate_pct": 1.0}

print("latency is judged against the run's own target")

t = build(4100, 0.0, SMOKE)
check("4100 ms passes a Smoke Test (target 5000)",
      not any("over the" in x for x in t), "got: %s" % t)

t = build(4100, 0.0, LOAD)
check("the same 4100 ms fails a Load Test (target 3000)",
      any("over the 3000ms target" in x for x in t), "got: %s" % t)

t = build(5200, 0.0, SMOKE)
check("and a Smoke Test still fails when it really is over",
      any("over the 5000ms target" in x for x in t), "got: %s" % t)

check("the constant is only a fallback",
      "over the %.0fms target" % DEFAULT_P95_THRESHOLD_MS
      in " ".join(build(DEFAULT_P95_THRESHOLD_MS + 100, 0.0, {})))

print()
print("so is the error budget")

t = build(100, 0.5, {"max_p95_ms": 5000, "max_error_rate_pct": 2.0})
check("0.5% passes a 2% budget",
      not any("Error rate" in x for x in t), "got: %s" % t)

t = build(100, 2.5, {"max_p95_ms": 5000, "max_error_rate_pct": 2.0})
check("2.5% fails it", any("exceeds the 2.00% SLA" in x for x in t), "got: %s" % t)

t = build(100, 0.5, SMOKE)
check("and the default 1% budget still bites at 1.5%",
      any("Error rate" in x for x in build(100, 1.5, SMOKE))
      and not any("Error rate" in x for x in t))

print()
print("a missing SLA does not crash the report")
for bad in ({}, {"max_p95_ms": None}, {"max_p95_ms": ""}, None):
    try:
        recommendation.build({"overall": {"p95": 9000, "error_rate": 0},
                              "endpoints": [], "sla": bad}, {}, None)
        ok = True
    except Exception as exc:
        ok, detail = False, str(exc)[:60]
    check("sla=%r is survivable" % (bad,), ok, "" if ok else detail)

print()
print("an id in a response is not evidence of an order")
# A run that stopped at 'Items added' and never reached checkout reported one
# order created, id "null". The call responsible was a billing-address popup:
#   POST /amnealcustomer/addressSelection/popupData
#   {"billingAddresses":[{"entity_id":"106","increment_id":null,...}]}
# _confirm_order scraped an id out of any body, and "increment_id": null
# matched. Rejecting nulls alone would not have been enough -- "entity_id":
# "106" in the same payload matches the next pattern.
import re as _re
_GEN = io.open("ltmetrics/agents/generator.py", encoding="utf-8").read()
_ns = {"re": _re}
_ns["_ORDER_URL_SIGNALS"] = ["checkout/success", "onepage/success"]
_ns["_ORDER_PLACE_PATTERNS"] = ["payment-information", "placeorder"]
exec(_re.search(r"^_NOT_AN_ID = (.+)$", _GEN, _re.M).group(0), _ns)
# _looks_like_order calls _places_orders, so the helper and its exclusion list
# have to be in the namespace before it is defined.
exec(_re.search(r"^_NOT_ORDER_PLACE = .+?\)\n", _GEN, _re.M | _re.S).group(0), _ns)
exec(_re.search(r"^def _places_orders.*?(?=^def )", _GEN, _re.M | _re.S).group(0), _ns)
for _fn in ("_is_real_id", "_extract_order_id", "_confirm_order",
            "_looks_like_order"):
    exec(_re.search(r"^def %s\(.*?(?=^def |^class |^_[A-Z])" % _fn, _GEN,
                    _re.M | _re.S).group(0), _ns)

_addr = ('{"billingAddresses":[{"entity_id":"106","increment_id":null,'
         '"parent_id":"40"}]}')
check("an address popup is not an order",
      _ns["_confirm_order"]("https://s/amnealcustomer/addressSelection/popupData",
                            200, _addr) is None)
check("nor is a cart id that happens to be a number",
      _ns["_confirm_order"]("https://s/rest/V1/carts/mine", 200, "15277") is None)
check("a JSON null is not an id", not _ns["_is_real_id"]("null"))
check("nor a boolean", not _ns["_is_real_id"]("false"))
check("a real id still is", _ns["_is_real_id"]("000001672"))

check("a place-order call with a real id is still counted",
      _ns["_looks_like_order"]("/rest/V1/carts/mine/payment-information",
                               200, "63022") == "63022")
check("a success page with no id in the body is still counted",
      _ns["_confirm_order"]("https://s/checkout/success/", 200,
                            "Thank you for your order") == "confirmed")
check("something other than the id has to say it is an order",
      "if not confirms:" in _GEN and "confirms = (any(s in u" in _GEN)

print()
print("a page placeholder is not a captured value")
# Amneal's add-to-cart was rejected with "Selected contract is not valid."
# Every contract field was correct; uenc went out as %2525uenc%2525. Magento
# renders cart links containing a literal /uenc/%25uenc%25/ for its own
# JavaScript to fill in, and the extractor captured that marker and injected
# it downstream, overwriting the real value the recording held.
exec(_re.search(r"^_PLACEHOLDER_RE = .+$", _GEN, _re.M).group(0), _ns)
exec(_re.search(r"^def _is_placeholder.*?(?=^def |^_[A-Z])", _GEN,
                _re.M | _re.S).group(0), _ns)
check("Magento's own uenc marker is recognised",
      _ns["_is_placeholder"]("%25uenc%25"))
check("so is the undecoded form", _ns["_is_placeholder"]("%uenc%"))
check("and a templating marker", _ns["_is_placeholder"]("${uenc}"))
check("a real uenc is not a placeholder",
      not _ns["_is_placeholder"]("aHR0cHM6Ly9tY3N0YWdpbmcuYW1uZWFsLmNvbQ"))
check("nor a form key", not _ns["_is_placeholder"]("MX3ZhFaJ4MTpGWzC"))
check("the capture skips it rather than storing it",
      "if m and _is_placeholder(m.group(1)):" in _GEN)
check("and says so, instead of failing silently",
      "keeping the recorded value" in _GEN)

print()
print("a 200 that refuses the request is not a success")
# The add-to-cart answered
#   HTTP 200 {"error":true,"error_messages":["Selected contract is not valid."]}
# and the report showed it green: 1 sample, 0 fails. The one call that broke
# the run was the one call the report said was fine.
for _c in ("_BODY_ERROR_RE", "_BODY_ERROR_MSG_RE"):
    exec(_re.search(r"^%s = re\.compile\(.*?\)\n" % _c, _GEN,
                    _re.M | _re.S).group(0), _ns)
exec(_re.search(r"^def _body_error.*?(?=^def |^_[A-Z])", _GEN,
                _re.M | _re.S).group(0), _ns)

check("a refused add-to-cart fails",
      _ns["_body_error"]('{"error":true,"error_messages":'
                         '["Selected contract is not valid."]}')
      == "Selected contract is not valid.")
check("and the store's own words are carried into the failure",
      "Selected contract" in _ns["_body_error"](
          '{"error":true,"error_messages":["Selected contract is not valid."]}'))
check("a GraphQL errors array fails too",
      _ns["_body_error"]('{"errors":[{"message":"Syntax Error"}]}') != "")
check("a success body passes",
      _ns["_body_error"]('{"success":true,"message":"Purchase order saved."}') == "")
check("error: false is not an error", _ns["_body_error"]('{"error":false}') == "")
check("nor is prose that mentions the word",
      _ns["_body_error"]("<p>An error occurred in our warehouse description</p>")
      == "")
check("Salesforce Commerce answers a refusal with a fault object",
      _ns["_body_error"]('{"fault":{"type":"X","message":"No such product"}}')
      == "No such product")
check("SOAP with a Fault element",
      _ns["_body_error"]("<soap:Fault><faultstring>Contract expired"
                         "</faultstring></soap:Fault>") == "Contract expired")
# Deliberately absent: {"status":"FAILED"}. It reads like an error envelope and
# appears just as often as data -- a list of past orders where one of them
# failed -- so matching it would fail steps that succeeded.
check("a list containing a failed order is not a failed request",
      _ns["_body_error"]('{"orders":[{"id":1,"status":"FAILED"}]}') == "")
check("nor is a field merely named faultTolerance",
      _ns["_body_error"]('{"faultTolerance":3}') == "")
check("an address payload is not an error",
      _ns["_body_error"]('{"billingAddresses":[{"entity_id":"106"}]}') == "")
check("both recorded-step paths apply it",
      _GEN.count("_berr = _body_error(txt) if ok else \"\"") == 2)
check("and the run says so rather than failing three steps later",
      "answered 200 but refused it" in _GEN)

print()
print("a setup step is not failed for not placing an order")
# Magento has two endpoints one word apart: set-payment-information sets the
# method and returns true; payment-information places the order. The pattern
# "payment-information" is a substring of both, so the replay loop treated the
# setup call as an order attempt and recorded a failure for a correct 200.
# Seven of the forty failures in a Radwell run were that.
_ns["_ORDER_PLACE_PATTERNS"] = ["payment-information", "placeorder",
                                "place-order", "purchaseorder/save"]
check("set-payment-information does not place an order",
      not _ns["_places_orders"]("/rest/uk/V1/carts/mine/set-payment-information"))
check("payment-information does",
      _ns["_places_orders"]("/rest/uk/V1/carts/mine/payment-information"))
check("nor does payment-methods",
      not _ns["_places_orders"]("/rest/uk/V1/carts/mine/payment-methods"))
check("a storefront placeOrder still does",
      _ns["_places_orders"]("/checkout/onepage/placeOrder"))
check("and the replay loop uses the same test",
      'if not _places_orders(s.get("path")):' in _GEN)

print()
print("both order paths accept the terms and conditions")
# Two code paths place an order over REST. Only the main one sent the checkout
# agreement ids, so every attempt on the other came back
#   400 "The order wasn't placed. First, agree to the terms and conditions"
# while the ids sat in a constant the tool had harvested from the recording.
check("there is one implementation", _GEN.count("def _agreement_ids(self)") == 1)
check("and both callers use it", _GEN.count("self._agreement_ids()") == 2)
check("the fallback path attaches them to the payment method",
      'pm["extension_attributes"] = {"agreement_ids": _agr}' in _GEN)
check("the recorded ids are preferred over a REST lookup",
      "RECORDING-FIRST" in _GEN and "_AGREEMENT_IDS" in _GEN)
check("the lookup is cached, not repeated per order",
      "_agr_cache" in _GEN)
check("no caller keeps its own copy of the lookup",
      _GEN.count('_EP["agreements_fallback"]') == 1)

print()
print("no cart, no order attempt")
# One Radwell iteration failed at Cart created ("The quote can't be created")
# and the run tried to place an order anyway, twice, on a cart that did not
# exist. Both came back "firstname is required" -- read from an address that
# was never fetched. One real failure became four reported ones, and the two
# loudest pointed at data that was complete.
check("the state machine is consulted before attempting an order",
      'if _st.get("stopped_at") and not _st.get("item_count"):' in _GEN)
check("and the reason it really stopped is what gets said",
      "not attempting an order" in _GEN and "stop_reason" in _GEN)
check("a checkout that DID fill a cart still falls back",
      "self._place_order()" in _GEN)

print()
print("both order paths accept the terms and conditions")
# Two code paths place an order over REST. Only the main one sent the checkout
# agreement ids, so every attempt on the other came back
#   400 "The order wasn't placed. First, agree to the terms and conditions"
# while the ids sat in a constant the tool had harvested from the recording.
check("there is one implementation", _GEN.count("def _agreement_ids(self)") == 1)
check("and both callers use it", _GEN.count("self._agreement_ids()") == 2)
check("the fallback path attaches them to the payment method",
      'pm["extension_attributes"] = {"agreement_ids": _agr}' in _GEN)
check("the recorded ids are preferred over a REST lookup",
      "RECORDING-FIRST" in _GEN and "_AGREEMENT_IDS" in _GEN)
check("the lookup is cached, not repeated per order",
      "_agr_cache" in _GEN)
check("no caller keeps its own copy of the lookup",
      _GEN.count('_EP["agreements_fallback"]') == 1)

print()
print("an expected 404 is not a load failure")
# An Amneal run placed six orders and still reported 10.7% errors. Eight of the
# fourteen failures could not have succeeded and were never required to.
check("carts/mine/totals is skipped when the validator drives checkout",
      '"carts/mine/totals"' in _GEN)
check("and the reason is recorded where the list is",
      "Current customer does not have an active cart" in _GEN)
check("the agreements call is marked as the probe it is",
      'self._rc("Checkout agreements", "GET",' in _GEN
      and "soft=True)" in _GEN)
check("soft still means what it said it meant",
      "NOT counted as a load" in _GEN)   # the docstring wraps mid-phrase

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
