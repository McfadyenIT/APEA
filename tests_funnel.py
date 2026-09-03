"""How many users reached each stage, and where they stopped.

The funnel needed one thing the log never had: which user made each call. A
stage can be counted from calls all day and still not say how many USERS got
that far.

The case this file cares most about is the run that CANNOT answer. Every run
recorded before this existed has no user number, so every call reads as user 0.
A funnel built from that shows one user reaching every stage -- a confident,
wrong picture of a real run. The endpoint must say it does not know, and the
page must say so in words rather than leaving an empty column, because a dash
in a "users" column reads as zero and zero is a different claim.

Run:  ./.venv/bin/python tests_funnel.py     # expect FAILURES: 0
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")

import ltmetrics.server as S  # noqa: E402

UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()
GEN = io.open("ltmetrics/agents/generator.py", encoding="utf-8").read()

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


def stages_for(rows):
    """Run the real endpoint over a synthetic call log."""
    d = tempfile.mkdtemp(prefix="ltm-funnel-")
    os.makedirs(os.path.join(d, "results"))
    with open(os.path.join(d, "results", "ltm_calls.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    orig = S._run_out_dir
    S._run_out_dir = lambda rid: d
    try:
        return S.run_stages("synthetic")
    finally:
        S._run_out_dir = orig


def call(stage, name, vu, ms=100, ok=True):
    return {"group": stage, "name": name, "vu": vu, "ms": ms, "ok": ok,
            "ts": 1000 + vu}


print("the identity reaches the log without costing a request")
check("the user carries a number", "_VU_SEQ" in GEN)
check("assigned once, when the user starts", "_VU_SEQ[0] += 1" in GEN)
check("handed over through Locust's own context", "def context(self):" in GEN)
check("and written onto every call", '"vu": (context or {}).get("vu", 0)' in GEN)

print()
print("three users, one drops at payment")
rows = ([call("Browse", "Home Page", v) for v in (1, 2, 3)]
        + [call("Login", "Login", v) for v in (1, 2, 3)]
        + [call("Payment", "Set payment", v) for v in (1, 2)]
        + [call("Order", "Order created", v) for v in (1, 2)])
d = stages_for(rows)
by = {s["stage"]: s for s in d["stages"]}
check("the funnel is available", d["funnel"] is True)
check("it saw three users", d["users"] == 3)
check("three reached Browse", by["Browse"]["users"] == 3)
check("three reached Login", by["Login"]["users"] == 3)
check("two reached Payment", by["Payment"]["users"] == 2)
check("and one is recorded as stopping there", by["Payment"]["dropped"] == 1)
check("no one is counted as stopping at the first stage",
      by["Browse"]["dropped"] == 0)
check("nobody stopped at Order", by["Order"]["dropped"] == 0)

print()
print("a run that cannot answer says so")
old = [dict(r, vu=0) for r in rows]          # a log from before this existed
d2 = stages_for(old)
check("the funnel is refused", d2["funnel"] is False)
check("users are null, not zero",
      all(s["users"] is None for s in d2["stages"]))
check("and dropped too", all(s["dropped"] is None for s in d2["stages"]))
check("the stage times still work",
      all(s["calls"] > 0 for s in d2["stages"]))

print()
print("the page follows suit")
check("columns appear only when the funnel is available",
      "const funnel = !!d.funnel" in UI)
check("and the reason is said in words when it is not",
      "does not record which user made each call" in UI)
check("the reason is recorded",
      "reads as zero" in UI or "different claim" in UI)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
