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
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
