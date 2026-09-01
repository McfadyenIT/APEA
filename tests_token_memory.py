"""Remembering a captured token, so losing the file stops losing the token.

A browser never tells a page where an uploaded file came from -- it reports a
fake path on purpose -- so APEA edits its own copy and the operator's file stays
as it was. Enrolment wrote tokens somewhere the next upload did not read, and
the two drifted apart twice in one day.

The guarantees that make a remembering store safe rather than surprising:

  * it never overwrites a token already in the file
  * it is keyed by STORE, so a staging token cannot reach another environment
  * it says when it restored something
  * it can be cleared, or a fresh token could never be captured again

Run:  ./.venv/bin/python tests_token_memory.py     # expect FAILURES: 0
"""
import io
import sys

sys.path.insert(0, ".")

from apea.agents import token_memory as TM  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name[:58], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


A = "https://mcstaging.radwell.eu/uk"
B = "https://other.example/uk"

TM.forget()                                   # start from nothing

print("a captured token is remembered and comes back")
TM.remember(A, {"u1@x.com": "tok1", "u2@x.com": "tok2"})
check("both were stored", len(TM.recall(A)) == 2)
rows = [{"username": "u1@x.com", "payment_token": ""}]
check("a blank cell is filled", TM.apply_to_rows(rows, A) == ["u1@x.com"])
check("with the right token", rows[0]["payment_token"] == "tok1")

print()
print("a token already in the file is never replaced")
rows = [{"username": "u1@x.com", "payment_token": "MINE"}]
check("nothing was filled", TM.apply_to_rows(rows, A) == [])
check("the operator's value survives", rows[0]["payment_token"] == "MINE")

print()
print("a token cannot cross between stores")
TM.remember(B, {"u3@x.com": "tok3"})
rows = [{"username": "u1@x.com", "payment_token": ""}]
check("store B does not hold store A's account", "u1@x.com" not in TM.recall(B))
check("and asking for a third store fills nothing",
      TM.apply_to_rows(rows, "https://third.example") == [])
check("because two stores are known, so there is nothing to infer",
      len(TM._load()) == 2)

print()
print("with exactly ONE store known, an unnamed target is unambiguous")
TM.forget(B)
rows = [{"username": "u1@x.com", "payment_token": ""}]
check("a blank target falls back to the only store",
      TM.apply_to_rows(rows, "") == ["u1@x.com"])

print()
print("it can be cleared, or a fresh token could never be captured")
check("one account", TM.forget(A, "u1@x.com") == 1)
check("that account is gone", "u1@x.com" not in TM.recall(A))
check("the other survives", "u2@x.com" in TM.recall(A))
check("a whole store", TM.forget(A) >= 1)
check("and everything", TM.forget() == 0 and TM.recall(A) == {})

print()
print("a broken store file is survivable, not fatal")
io.open(TM._STORE, "w", encoding="utf-8").write("{ not json")
check("a corrupt file reads as empty", TM.recall(A) == {})
check("and writing over it recovers", TM.remember(A, {"u9@x.com": "t9"}) == 1)
TM.forget()

print()
print("the wiring exists on both sides")
srv = io.open("apea/server.py", encoding="utf-8").read()
enr = io.open("enrol_cards.py", encoding="utf-8").read()
ui = io.open("apea/static/index.html", encoding="utf-8").read()
check("enrolment records what it captured", "token_memory.remember" in enr)
check("upload restores into blank cells", "token_memory.apply_to_rows" in srv)
check("the restore is reported to the page", '"restored_tokens"' in srv)
check("and the page says it out loud", "restored_tokens" in ui and "Put back" in ui)
check("the store is recorded where it is unambiguous",
      '_LAST_TARGET["url"] = req.base_url' in srv)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
