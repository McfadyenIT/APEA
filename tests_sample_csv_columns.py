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
SRC = io.open("apea/agents/parameterization.py", encoding="utf-8").read()


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
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
