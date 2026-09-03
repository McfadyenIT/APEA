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
print("the existing sku guarantee is untouched")
check("sku is still always offered", 'if "sku" not in columns:' in SRC)

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
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
