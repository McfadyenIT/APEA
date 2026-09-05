"""Regression guard: a transaction timer is not an endpoint, and a percentile
is never above the maximum.

Run after any change to ltmetrics/agents/analyzer.py, recommendation.py, or the
endpoint table in ltmetrics/reporting.py:

    ./.venv/bin/python tests_transaction_verdict.py     # expect: FAILURES: 0

A transaction timer wraps the calls inside it, so its time is their sum. Held to
a PER-REQUEST gate, any journey of more than a handful of calls breaches by
existing -- and then leads the Jira ticket as the latency hotspot.

Two live runs on 2026-09-04 showed both halves of the damage:

  Amneal  eb762c912965  the ONLY endpoint breach over the plan gate was
                        "TXN: Test" (p95 18,000ms), a wrapper around 21 calls.
                        It headed the ticket. Home Page 3,100ms vs its own
                        800ms target is the real one, and it is kept.
  Radwell e0872e29b59f  three breaches, of which "TXN: Login" (8,700ms) merely
                        restated "Login" (8,500ms) and "TXN: Checkout" was the
                        sum of the checkout calls. One real finding, reported
                        three times.

The same reports showed p99 18,000ms against a max of 17,908.9ms. Locust buckets
its percentiles, so p99 can land above the exact maximum it was measured from. A
reader who notices that stops trusting the whole table.

Transactions keep their row, their numbers and their failures. What they lose is
a verdict they were never eligible for.
"""
import csv
import inspect
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ltmetrics.agents import analyzer, recommendation   # noqa: E402

fails = []


def check(label, ok):
    print("   %-64s %s" % (label[:64], "OK" if ok else "FAIL"))
    if not ok:
        fails.append(label)


COLS = ["Type", "Name", "Request Count", "Failure Count", "Median Response Time",
        "Average Response Time", "Min Response Time", "Max Response Time",
        "Average Content Size", "Requests/s", "Failures/s",
        "50%", "66%", "75%", "80%", "90%", "95%", "98%", "99%", "99.9%",
        "99.99%", "100%"]


def _row(rtype, name, p95, p99, mx, reqs=3, fails_=0):
    r = dict.fromkeys(COLS, "0")
    r.update({"Type": rtype, "Name": name, "Request Count": str(reqs),
              "Failure Count": str(fails_), "Average Response Time": "500",
              "Min Response Time": "100", "Max Response Time": str(mx),
              "Requests/s": "0.5", "50%": "400", "90%": str(p95),
              "95%": str(p95), "99%": str(p99)})
    return r


def _run_dir(rows):
    d = Path(tempfile.mkdtemp(prefix="ltm-txn-"))
    (d / "results").mkdir(parents=True)
    with io.open(d / "results" / "locust_stats.csv", "w",
                 encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return d


print("\n== a transaction is recognised as one ==")
check("by its Locust type",
      analyzer.is_transaction({"method": "TXN", "name": "whatever"}))
check("by its name prefix, for a run recorded before the type was set",
      analyzer.is_transaction({"method": "POST", "name": "TXN: Checkout"}))
check("and a real endpoint is not one",
      not analyzer.is_transaction({"method": "POST", "name": "Order created"}))
check("nor is an endpoint that merely mentions it",
      not analyzer.is_transaction({"method": "GET", "name": "Transaction history"}))

print("\na percentile is never above the maximum")
d = _run_dir([_row("GET", "Home Page", 3100, 18000, 17908.9),
              _row("GET", "Aggregated", 3300, 18000, 17908.9)])
eps, agg = analyzer._read_stats(d)
check("p99 is pulled back to the max it was measured from",
      eps and eps[0]["p99"] <= eps[0]["max"])
check("so is p95", eps and eps[0]["p95"] <= eps[0]["max"])
check("and the aggregate row follows", agg and agg["p99"] <= agg["max"])
check("a percentile below the max is left alone", eps and eps[0]["p95"] == 3100)

print("\nthe verdict goes to endpoints, not to journeys")
src = inspect.getsource(analyzer.analyze)
check("the gate loop asks whether the row is a transaction",
      "is_transaction(e)" in src)
check("and gives it no target rather than a failing one",
      'e["sla_target"] = None' in src)
check("a transaction never reaches the breach list",
      src.index("is_transaction(e)") < src.index("breaches.append"))

print("\nthe root-cause line names an endpoint too")
# This one feeds the Jira ticket, so it is the copy a client actually reads.
# It kept its own latency sort after the SLA table and the recommendation had
# both been fixed, and so still reported "Latency leader: TXN: Test (21000 ms)"
# beside a recommendation naming POST inventory/validate at 6397 ms.
rca_src = inspect.getsource(analyzer._rca)
check("the hotspot sort excludes transactions",
      "is_transaction(e)" in rca_src)
check("but a run of nothing but transactions still gets an answer",
      "_real or endpoints" in rca_src)
check("failures are still counted against a transaction",
      'sorted(endpoints, key=lambda x: -x["num_failures"])' in rca_src)

print("\nthe slowest ENDPOINT is an endpoint")
src = inspect.getsource(recommendation)
check("the slowest-endpoint pick skips rows with no target",
      'e.get("sla_target") is not None' in src)

print("\nthe per-endpoint latency cards skip transactions too")
# The fourth place that sorts by p95 against a target, and the last one found.
# It read LABEL_SLA directly instead of the sla_target the gate had already
# decided, so a Radwell run showed three cards -- TXN: Checkout, TXN: Login and
# Login -- for one bottleneck, two of them advising a CDN for a stopwatch.
rec_src = inspect.getsource(analyzer._recommend)
check("the card list filters on the gate's own target",
      'e.get("sla_target") is not None' in rec_src)
check("and compares against that target, not a second lookup",
      'e["p95"] > e["sla_target"]' in rec_src)
check("the title quotes the same target it filtered on",
      "int(e['sla_target'])" in rec_src)
check("no second LABEL_SLA lookup left to drift",
      "LABEL_SLA.get(e['name']" not in rec_src
      and 'LABEL_SLA.get(e["name"]' not in rec_src)

print("\nthe report renders an absent verdict as absent")
UI = io.open(ROOT / "ltmetrics" / "reporting.py", encoding="utf-8").read()
check("no cross where there is no target",
      '"—" if _tgt is None else ("✅"' in UI)
check("no target column either", '{"—" if _tgt is None else int(_tgt)}' in UI)
check("the spreadsheet says n/a rather than FAIL",
      '"n/a" if e.get("sla_target") is None' in UI)
check("and the table explains what a dash means",
      "a transaction" in UI and "per-request target does not apply" in UI)

print("\nFAILURES: %d" % len(fails))
for f in fails:
    print("  - %s" % f)
sys.exit(1 if fails else 0)
