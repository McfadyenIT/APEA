"""The CSV APEA generates must be usable without hand-editing.

A tester uploads a recording, downloads the sample data file, fills in accounts,
and runs. Every column that flow needs has to be IN that file. Two were not:

  * payment_token -- the column the whole card path reads and enrolment writes
  * company       -- required by checkouts that ask for a business name

and one was there that must not be: card_number. The store never receives a card
number, nothing reads that column, and a column with that name in a spreadsheet
is exactly where a real card ends up.

Run:  ./.venv/bin/python tests_generated_columns.py    # expect FAILURES: 0
"""
import io
import re
import sys

sys.path.insert(0, ".")

FAILURES = []
SRC = io.open("apea/agents/test_data_generator.py", encoding="utf-8").read()


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name[:58], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


m = re.search(r"_COLUMNS = \[(.*?)\]", SRC, re.S)
assert m, "_COLUMNS not found"
COLS = [c.strip().strip('"') for c in m.group(1).replace("\n", " ").split(",") if c.strip()]

print("every column the documented workflow reads is generated")
for col, why in (
        ("username", "sign in"),
        ("password", "sign in"),
        ("firstname", "checkout address"),
        ("lastname", "checkout address"),
        ("company", "required by checkouts that ask for a business name"),
        ("street", "checkout address"),
        ("city", "checkout address"),
        ("postcode", "checkout address"),
        ("telephone", "checkout address"),
        ("country_id", "checkout address"),
        ("region", "checkout address"),
        ("product_id", "find the product"),
        ("sku", "find the product"),
        ("search_keyword", "find the product"),
        ("qty", "basket"),
        ("payment_method", "how each row pays"),
        ("payment_token", "the card code enrolment writes"),
        ("card_cvv", "security code"),
        ("po_number", "invoice payers")):
    check("%s (%s)" % (col, why), col in COLS)

print()
print("the card-number column is gone, and stays gone")
check("card_number is not a generated column", "card_number" not in COLS)
check("no card number is written into a row", 'row["card_number"]' not in SRC)
check("the reason is recorded where the list is",
      "never receives a card number" in SRC)

print()
print("card intent is judged on the token, not on a card number")
check("detection reads payment_token", 'r.get("payment_token")' in SRC)
check("validation reads payment_token",
      'str(r.get("payment_token") or "").strip() for r in rows' in SRC)
check("the failure names the remedy", "enrol_cards.py" in SRC)
check("and names the alternative", "offline payment method" in SRC)

print()
print("the token is left empty on purpose")
check("payment_token is never given an invented value",
      'row["payment_token"] =' not in SRC)
check("the reason is written down", "fail closed" in SRC)

print()
print("company has a default so a fresh file works unedited")
check("a default company exists", "_DEFAULT_COMPANY" in SRC)
check("it is applied to every row", 'row["company"] = _DEFAULT_COMPANY' in SRC)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
