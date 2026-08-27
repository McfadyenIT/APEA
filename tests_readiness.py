"""Regression guard: the run must be sized by ACCOUNTS, not by rows, and the
readiness panel must say what will happen before the run, not after.

Run after any change to _account_summary in apea/server.py or the readiness
panel in apea/static/index.html:

    ./.venv/bin/python tests_readiness.py     # expect: FAILURES: 0

Why it exists. A platform cart belongs to the CUSTOMER, so two virtual users
signed in as the same account contend for one basket. On Magento that surfaces
as "The quote can't be created." -- a functional failure that pollutes the
measurement rather than an interesting result. It cost a whole afternoon on a
live store before it was understood.

So the safe number of concurrent users is the number of DISTINCT LOGINS in the
data file, not the number of rows. A file with 10 rows across 2 accounts
supports 2 concurrent users, not 10.

The same panel reports which accounts can actually pay by card, because a
stored-card token belongs to one customer: an account with a blank token in a
run that declared a card is a run that CANNOT honour what it declared, and
since the fail-closed change that fails rather than quietly billing on account.
Saying so before the run is the whole point.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from apea.server import _account_summary, _read_csv_rows   # noqa: E402

fails = []


def check(label, got, want):
    ok = got == want
    print("   %-62s %-6s %s" % (label[:62], "OK" if ok else "FAIL",
                                "" if ok else "(got %r want %r)" % (got, want)))
    if not ok:
        fails.append(label)


def summarise(csv_text):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8", newline="") as fh:
        fh.write(csv_text)
        path = fh.name
    return _account_summary(*_read_csv_rows(path))


print("Concurrency is bounded by distinct logins, not by rows")
a = summarise(
    "username,product_id,payment_token\n"
    "alice@x.com,1,TOK-A\n"
    "alice@x.com,2,TOK-A\n"
    "alice@x.com,3,TOK-A\n"
    "bob@x.com,1,TOK-B\n"
    "bob@x.com,2,TOK-B\n")
check("5 rows are counted", a["rows"], 5)
check("but only 2 accounts", a["unique_logins"], 2)
check("so 2 is the safe concurrency", a["safe_concurrent_users"], 2)
check("distinct products are counted separately", a["products"], 3)

print("\nIt reports who can pay by card, per account")
a = summarise(
    "username,product_id,payment_token\n"
    "alice@x.com,1,TOK-A\n"
    "bob@x.com,1,\n"
    "carol@x.com,1,   \n")
check("one account has a token", a["with_token"], ["alice@x.com"])
check("two do not", a["without_token"], ["bob@x.com", "carol@x.com"])
check("whitespace is not a token", "carol@x.com" in a["without_token"], True)

print("\nA token on ANY row counts for that account")
a = summarise(
    "username,product_id,payment_token\n"
    "alice@x.com,1,\n"
    "alice@x.com,2,TOK-A\n")
check("alice can pay by card", a["with_token"], ["alice@x.com"])

print("\nIt copes with a file that has no accounts at all")
a = summarise("product_id,qty\n1,1\n2,1\n")
check("no logins", a["unique_logins"], 0)
check("rows still counted", a["rows"], 2)
check("no token column is reported as such", a["has_token_column"], False)

print("\nemail is accepted as the login column")
a = summarise("email,product_id\nalice@x.com,1\n")
check("email counts as a login", a["unique_logins"], 1)

print("\nThe panel exists and reads the right things")
html = (ROOT / "apea" / "static" / "index.html").read_text(encoding="utf-8")
check("there is a readiness panel", 'id="readiness"' in html, True)
check("it warns when users exceed accounts",
      "users but only" in html, True)
check("and explains the consequence in plain words",
      "share one basket" in html, True)
check("it names accounts that cannot pay by card", "no card for" in html, True)
check("and says the run will FAIL rather than substitute",
      "rather than" in html and "quietly billing on account" in html, True)
check("it reflects the business-critical call selection",
      "business-critical call" in html, True)

print("\nA suggested user count never overwrites an explicit one")
check("the suggestion is only applied to an untouched field",
      "!uEl.dataset.userSet" in html, True)
check("and typing marks the field as explicitly set",
      "u.dataset.userSet = '1'" in html, True)
check("the panel refreshes when the count changes",
      "u.addEventListener('input'" in html, True)

print("\nThe method the recording used is preselected")
check("preselected only when the recording used exactly one",
      "methods.length === 1" in html, True)
check("and never over an explicit choice",
      "!pmEl.dataset.userSet" in html, True)

print("\nThe card template is only offered when the data can honour it")
check("it checks the CSV for a token first", "dataHasToken" in html, True)
check("and explains why it stayed blank",
      "would fail for every user" in html, True)

print("\nThe panel distinguishes method-selected from card-declared")
check("a card is DECLARED only when the token reference is present",
      "cardDeclared" in html and "addl.indexOf('{{')" in html, True)
check("method without details gets its own message",
      "card method but no card" in html, True)
check("a warning does not sit under a Ready heading",
      "(bad || warn) ? 'Before you run'" in html, True)

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
