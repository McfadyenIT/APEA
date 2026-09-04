"""The CSV a tester downloads after analysing a recording must be complete.

Its columns come from the RECORDING -- a column exists because its value crossed
the wire. That is the right default and it has a blind spot: a field the recording
never shows produces no column, and the tester finds out when checkout stops.

Two fields are invisible that way and both block a run:

  company        marked required by checkouts that ask for a business name, and
                 never typed during a recording made on an account with a saved
                 address
  payment_token  minted per ACCOUNT at checkout, so one recording can only ever
                 reveal one, while a pool needs one each

Both are now always offered, the way `sku` already was.

Run:  ./.venv/bin/python tests_sample_csv_columns.py    # expect FAILURES: 0
"""
import io
import sys

sys.path.insert(0, ".")

FAILURES = []
SRC = io.open("ltmetrics/agents/parameterization.py", encoding="utf-8").read()


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name[:58], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


print("both blind-spot columns are always offered")
check("company is added when the recording did not reveal it",
      '("company", "Address"' in SRC)
check("payment_token is added when the recording did not reveal it",
      '("payment_token", "Card"' in SRC)
check("neither is added twice", 'if _extra in columns:' in SRC
      and "continue" in SRC.split('if _extra in columns:')[1][:40])

print()
print("they arrive EMPTY, and that is the point")
check("no invented value is written for either",
      'value_by_col["company"] =' not in SRC
      and 'value_by_col["payment_token"] =' not in SRC)
check("the reason is recorded next to the code", "fail closed" in SRC)

print()
print("empty must not block the run")
opt = SRC.split("_OPTIONAL_COLUMNS = {")[1].split("}")[0]
check("company is optional, so a blank does not block", '"company"' in opt)
check("payment_token is optional, so a blank does not block",
      '"payment_token"' in opt)

print()
print("the group they appear under makes sense to a reader")
check("an Address group explanation exists", '"Address":' in SRC)
check("the Card group already existed", '"Card":' in SRC)

print()
print("the sku guarantee now asks whether there is a basket")
# It used to be unconditional, which offered a stored-card token and a sku to a
# bank transfer. The guarantee it was written for -- a run needs something to
# put in a basket even when the recording names it oddly -- still holds
# wherever there IS a basket.
check("sku is offered whenever the recording has a cart or a checkout",
      'if _sells and "sku" not in columns:' in SRC)
check("and what counts as a basket is spelt out",
      '"cart", "basket", "checkout", "catalog", "/product"' in SRC)
check("a payment endpoint alone is deliberately not enough",
      "a bank transfer posts to" in SRC)

print()
print("a sample value has to be a value")
# The Amneal sample CSV carried a GraphQL query body as its search keyword:
#   {       cmsBlocks(identifiers: ["no-search-category-block"]) {   items {
# _clean_sample flattened the newlines and cut it to 80 characters, which is
# how a request body came to look like something a person had typed.
import sys as _sys
_sys.path.insert(0, ".")
from ltmetrics.agents.parameterization import _clean_sample as _cs

check("a request body is not offered as a sample",
      _cs('{  cmsBlocks(identifiers: ["no-search-category-block"]) { items {') == "")
check("nor is JSON", _cs('{"query":"x"}') == "")
check("nor is markup", _cs("<div>hi</div>") == "")
check("a recorded telephone number survives its brackets",
      _cs("+1 (354) 643-6356") == "+1 (354) 643-6356")
check("so does an ordinary address", _cs("1000 QUALITY DRIVE") == "1000 QUALITY DRIVE")
check("and an email", _cs("dartmouth@yopmail.com") == "dartmouth@yopmail.com")
check("a rejected value leaves the cell empty, which already means 'not "
      "supplied'", _cs("{}") == "")

print()
print("a column is not bound to a field that carries a request body")
# GraphQL puts its whole document in a field named "query", which matches the
# search pattern. search_keyword was bound to it, so filling that column
# replaced the query with the keyword:
#   POST graphql -> 400 Syntax Error: Unexpected Name "triamcinolone"
# Stopping the blob appearing as a SAMPLE was not enough: the binding stayed,
# so whatever the operator typed went to the same place.
_PAR = io.open("ltmetrics/agents/parameterization.py", encoding="utf-8").read()
check("the recorded value is judged before the column is bound",
      "_recorded = field_names.get(name)" in _PAR
      and "not _clean_sample(_recorded)" in _PAR)
check("a field the recording left empty still binds",
      '_recorded not in (None, "")' in _PAR)
check("the guard runs inside the field loop, before the mapping",
      _PAR.index("_recorded = field_names.get(name)")
      < _PAR.index("for pat, group, col, sample in _FIELD_MAP"))

print()
print("a value the recording submitted is offered, whatever its name")
# contract_id was not a known field name, so nothing offered it and every run
# posted a contract the store had stopped accepting. The same trap waits for
# every field the name-map does not know.
from ltmetrics.agents import parameterization as _pm


def _an(steps):
    r = _pm.analyze(steps, None)
    return r.get("columns") or [], [g.get("group") for g in (r.get("groups") or [])]

_fed, _fedg = _an([
    {"method": "POST", "path": "/ship/v1/shipments",
     "body": {"accountNumber": "510087020", "serviceType": "PRIORITY_OVERNIGHT",
              "packageWeight": "2.5"}},
    {"method": "POST", "path": "/track/v1/trackingnumbers",
     "body": {"trackingNumber": "794658123456"}}])
check("a logistics recording gets its own fields",
      {"accountNumber", "serviceType", "trackingNumber"} <= set(_fed))
check("a 12-digit tracking number is not mistaken for a timestamp",
      "trackingNumber" in _fed)
check("they are grouped as what they are",
      "Other recorded values" in _fedg)
check("and it is not offered a basket it does not have",
      not ({"sku", "payment_token", "product_id"} & set(_fed)))

_bank, _ = _an([{"method": "POST", "path": "/payments/new",
                 "body": {"fromAccount": "12345678", "sortCode": "20-00-00",
                          "amount": "150.00"}}])
check("a bank transfer gets its own fields",
      {"fromAccount", "sortCode", "amount"} <= set(_bank))
check("a payment endpoint alone is not a shop",
      not ({"sku", "payment_token"} & set(_bank)))

_shop, _ = _an([
    {"method": "POST", "path": "/customer/account/loginPost",
     "body": {"login[username]": "a@b.com", "login[password]": "x"}},
    {"method": "POST", "path": "/checkout/cart/add",
     "body": {"product": "429", "qty": "1"}}])
check("a real shop still gets the commerce columns",
      {"sku", "product_id", "search_keyword", "payment_token"} <= set(_shop))

_PAR2 = io.open("ltmetrics/agents/parameterization.py", encoding="utf-8").read()
check("URLs are not offered as values", '"://" in _val' in _PAR2)
check("nor epochs", 'len(_val) in (10, 13)' in _PAR2)
check("nor a second copy of the credentials",
      "pass(word|wd)?$|email" in _PAR2)
check("and the list is capped so the file stays readable",
      "_others[:12]" in _PAR2)

print()
print("a column pre-filled from the recording does not block a run")
# Offering the contract fields and the other recorded values made every one of
# them REQUIRED, because anything off a fixed optional list blocks. An existing
# Radwell data file was refused for not having a column called
# super_attribute_553 -- a column that had not existed until that morning.
_flow = [{"method": "POST", "path": "/checkout/cart/add",
          "body": {"product": "429", "qty": "1", "contract_id": "VIZDSHNPBIO",
                   "super_attribute_553": "77", "backorder": "0"}},
         {"method": "POST", "path": "/customer/account/loginPost",
          "body": {"login[username]": "a@b.com", "login[password]": "x"}}]
_res = _pm.analyze(_flow, None)
_req = set(_res.get("required_columns") or [])
check("the offered columns are not required",
      not ({"super_attribute_553", "backorder"} & _req))
check("nor are the contract fields",
      not ({"contract_id", "price_group_id"} & _req))
check("credentials still are", {"username", "password"} <= _req)

# validate() re-derived blocking from its own list, so correcting analyze alone
# left the uploader still refusing the file. One place decides now.
_rows = [{"username": "a@b.com", "password": "x", "product_id": "429",
          "sku": "ABC"}]
_v = _pm.validate(_res.get("columns"), _rows,
                  required=_res.get("required_columns"))
check("so a file without them validates", _v.get("ok") is True)
check("and nothing blocks", _v.get("blocking") is False)
_old = _pm.validate(_res.get("columns"), _rows)
check("called without it, the old behaviour is unchanged",
      isinstance(_old.get("ok"), bool))

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
