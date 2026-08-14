"""Offline self-test for the DYNAMIC sample-data generator.

Proves the sample CSV is built from what the recording actually contained — not
from opinionated hardcoded defaults — and that required-but-missing values are
flagged with a <<FILL: …>> sentinel that validation treats as empty (so an
unfilled sample stays blocked).

Runs with NO server and NO network. From the project root:

    python test_sample_data.py

Exits 0 if every check passes, 1 otherwise.
"""
import sys

from apea.agents import parameterization as pz

_fails = 0


def check(name, cond, detail=""):
    global _fails
    ok = bool(cond)
    if not ok:
        _fails += 1
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "   << " + str(detail)))
    return ok


# A recording carrying REAL values (password intentionally left blank so we can
# exercise the required-<<FILL>> path for a required column that has no value).
flow = [
    {"method": "POST", "path": "/uk/rest/uk/V1/integration/customer/token",
     "body": '{"username":"joe@acme.com","password":""}'},
    {"method": "POST", "path": "/uk/rest/uk/V1/carts/mine/items",
     "body": '{"cartItem":{"sku":"WIDGET-9","qty":2}}'},
    {"method": "POST", "path": "/uk/rest/uk/V1/carts/mine/payment-information",
     "body": ('{"paymentMethod":{"method":"netterms"},'
              '"billingAddress":{"firstname":"Joe","lastname":"Bloggs",'
              '"street":["1 High St"],"city":"Leeds","postcode":"LS1 1AA",'
              '"countryId":"GB","telephone":"01130000000","region":"West Yorkshire"}}')},
]

an = pz.analyze(flow)
csv = an["sample_csv"]

print("\n1) Sample values come from the recording (not hardcoded defaults)")
check("payment_method uses recorded 'netterms'", "netterms" in csv, csv)
check("no hardcoded 'checkmo'", "checkmo" not in csv, csv)
check("city uses recorded 'Leeds'", "Leeds" in csv, csv)
check("no hardcoded 'London'", "London" not in csv, csv)
check("no hardcoded test card 4111...", "4111111111111111" not in csv, csv)
check("username uses recorded 'joe@acme.com'", "joe@acme.com" in csv, csv)
check("region uses recorded 'West Yorkshire'", "West Yorkshire" in csv, csv)
check("product id uses recorded 'WIDGET-9'", "WIDGET-9" in csv, csv)

print("\n2) Required-but-missing values are highlighted with a <<FILL: …>> sentinel")
check("password (required, empty in recording) is flagged",
      "<<FILL: password" in csv, csv)
check("password reported as required in groups meta",
      any(f.get("column") == "password" and f.get("required")
          for g in an["groups"] for f in g.get("fields", [])))
check("required_columns lists password", "password" in an.get("required_columns", []),
      an.get("required_columns"))

print("\n3) Validation treats a leftover sentinel as empty (stays blocked)")
row_unfilled = {"username": "joe@acme.com", "password": "<<FILL: password — required>>",
                "product_id": "WIDGET-9"}
v1 = pz.validate(an["columns"], [row_unfilled])
check("unfilled sentinel -> BLOCKED", v1["ok"] is False, v1["messages"])
check("message tells user to replace <<FILL>>",
      any("FILL" in m for m in v1["messages"]), v1["messages"])

print("\n4) A genuinely filled row passes")
row_filled = dict(row_unfilled, password="S3cret!")
v2 = pz.validate(an["columns"], [row_filled])
check("real value -> OK", v2["ok"] is True, v2["messages"])

print("\n5) CSV is safely quoted (recorded values with commas don't break columns)")
flow2 = [{"method": "POST", "path": "/x",
          "body": '{"street":["1 High St, Unit 4"],"username":"a@b.com"}'}]
csv2 = pz.analyze(flow2)["sample_csv"]
header = csv2.splitlines()[0].split(",")
check("street column present", "street" in header, header)
check("embedded comma is quoted, not a new column",
      '"1 High St, Unit 4"' in csv2, csv2)

print("\n" + ("ALL CHECKS PASSED" if _fails == 0 else "%d CHECK(S) FAILED" % _fails))
sys.exit(1 if _fails else 0)
