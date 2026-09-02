"""Getting a FRESH card token when the data file already carries one.

Before this there was no way to do it. Forgetting the remembered copy left the
token in the file's own cell; clearing the cell let the next upload refill it
from memory. Each undid the other, so "capture a fresh one" was advice nobody
could follow -- an operator asked three times where the control was, and the
honest answer was that it did not exist.

Clearing both is the only thing that works, so the two must happen together.

Run:  ./.venv/bin/python tests_fresh_tokens.py     # expect FAILURES: 0
"""
import csv
import io
import os
import sys
import tempfile

sys.path.insert(0, ".")

from ltmetrics.server import _clear_tokens  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


FIELDS = ["username", "password", "payment_method", "payment_token", "qty"]


def make_csv(rows):
    """A throwaway data file. Never the operator's own -- a test of mine has
    already destroyed a real token once."""
    path = os.path.join(tempfile.mkdtemp(prefix="fresh-"), "pool.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), {r["username"]: r for r in reader}


ROWS = [
    {"username": "a@x.invalid", "password": "1", "qty": "1",
     "payment_method": "paradoxlabs_cybersource", "payment_token": "aaaa1111"},
    {"username": "b@x.invalid", "password": "2", "qty": "2",
     "payment_method": "paradoxlabs_cybersource", "payment_token": "bbbb2222"},
    {"username": "c@x.invalid", "password": "3", "qty": "3",
     "payment_method": "netterms", "payment_token": ""},
]

print("clearing the cell that would refill the memory")

p = make_csv(ROWS)
n = _clear_tokens(p, ["a@x.invalid"])
fields, after = read_csv(p)
check("it clears exactly the account named", n == 1)
check("that cell is empty", after["a@x.invalid"]["payment_token"] == "")
check("another account keeps its token",
      after["b@x.invalid"]["payment_token"] == "bbbb2222")
check("every column survives the rewrite", fields == FIELDS)
check("every other value survives",
      after["a@x.invalid"]["qty"] == "1"
      and after["b@x.invalid"]["password"] == "2"
      and after["c@x.invalid"]["payment_method"] == "netterms")
check("no row is lost", len(after) == 3)

p = make_csv(ROWS)
check("several accounts at once",
      _clear_tokens(p, ["a@x.invalid", "b@x.invalid"]) == 2)

p = make_csv(ROWS)
check("an account with no token counts as nothing to clear",
      _clear_tokens(p, ["c@x.invalid"]) == 0)
check("an unknown account changes nothing",
      _clear_tokens(make_csv(ROWS), ["nobody@x.invalid"]) == 0)
check("matching ignores case and spacing",
      _clear_tokens(make_csv(ROWS), ["  A@X.Invalid "]) == 1)
check("an empty list is a no-op", _clear_tokens(make_csv(ROWS), []) == 0)
check("so is no list at all", _clear_tokens(make_csv(ROWS), None) == 0)
check("a missing file is refused quietly",
      _clear_tokens("/no/such/file.csv", ["a@x.invalid"]) == 0)

# A file that has no payment_token column at all -- built here rather than
# reusing the fixture, so the check does what its name says.
_nocol = os.path.join(tempfile.mkdtemp(prefix="fresh-"), "nocol.csv")
with open(_nocol, "w", newline="", encoding="utf-8") as _fh:
    _w = csv.DictWriter(_fh, fieldnames=["username", "qty"])
    _w.writeheader()
    _w.writerow({"username": "a@x.invalid", "qty": "1"})
check("a file with no token column is left alone",
      _clear_tokens(_nocol, ["a@x.invalid"]) == 0)
check("and is not rewritten",
      io.open(_nocol, encoding="utf-8").read().count(",") == 2)

print()
print("the endpoint and the page offer it")
_srv = io.open("ltmetrics/server.py", encoding="utf-8").read()
check("the request can name a data file", "data_csv: Optional[str] = None" in _srv)
check("forgetting also clears those cells",
      "_clear_tokens(req.data_csv, targets)" in _srv)
check("and reports both numbers", '"cleared": cleared' in _srv)
check("it rewrites LT Metrics's copy, never the operator's",
      "uploaded copy, never the operator" in _srv)

_ui = io.open("ltmetrics/static/index.html", encoding="utf-8").read()
check("the ready state offers it", "btnFreshTokens" in _ui)
check("it is wired when the panel is drawn", "wireFresh();" in _ui)
check("it sends the accounts that have tokens", "usernames: have" in _ui)
check("and the uploaded file", "data_csv: uploads.data_csv" in _ui)
check("then redraws so the Fetch button appears",
      "await validateData();" in _ui.split("wireFresh")[1][:1200])

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
