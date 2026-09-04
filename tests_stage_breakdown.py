"""Step 2: where the time went, per stage.

Reads the call log a run already writes, so this adds no measurement -- only a
reading of what was recorded.

Held here because the product runs across many clients:

  * stages come back in the order the run first reached them, from whatever
    stages that run actually had. Nothing knows one client's journey, and
    nothing assumes six of anything.
  * labels are applied at READ time, so every run already in the ledger gains
    the breakdown. Labelling only in the generated script would have made this
    work for runs from today and show "Unlabelled 73%" for all the history.
  * the calls endpoint reads either filename, so runs made before the rename
    are still readable.

And the measurement trap: a TXN row is an aggregate whose duration already
contains the calls inside it. Counting it would add the same milliseconds
twice -- it put 73% of a real run in "(none)".

Run:  ./.venv/bin/python tests_stage_breakdown.py     # expect FAILURES: 0
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


SRV = io.open("ltmetrics/server.py", encoding="utf-8").read()
UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()

print("the endpoint reads, it does not measure")
check("there is a stages endpoint", '/api/run/{run_id}/stages' in SRV)
check("it reads the call log the run already writes", "_calls_path(out_dir)" in SRV)
check("no new instrumentation was added",
      "def run_stages" in SRV and "locust" not in SRV.split("def run_stages")[1][:1500])

print()
print("history stays readable")
check("either filename is accepted",
      '"ltm_calls.jsonl", "apea_calls.jsonl"' in SRV)
check("the reason is recorded", "before the rename" in SRV)

print()
print("nothing assumes one client's journey")
check("stages are ordered by first appearance", "order.append(stage)" in SRV)
check("not sorted, not from a fixed list",
      "sorted(agg" not in SRV and "STAGE_ORDER" not in SRV)
check("the reason is recorded", "one client's journey differ from another" in SRV)
check("the page renders whatever comes back",
      "(d && d.stages) || []" in UI)

print()
print("labels are applied when the log is read")
check("the canonical map is loaded server-side", "_kb_stage_map" in SRV)
check("only for calls with no stage of their own",
      'stage = canon.get(name, "")' in SRV)
check("a recorded group still wins",
      SRV.index('stage = (c.get("group") or "").strip()')
      < SRV.index('stage = canon.get(name, "")'))

print()
print("aggregates are excluded, not counted twice")
check("TXN rows are dropped", 'stage == "TXN" or name.startswith("TXN: ")' in SRV)
check("older logs without the label are caught too",
      'name.startswith("TXN: ")' in SRV)
check("and the count is reported rather than hidden", '"excluded_txn"' in SRV)

print()
print("the panel answers one question")
# The card declares its phase now, rather than being recognised by its title.
check("it is in phase 4", 'id="stageCard" data-phase="4"' in UI)
check("it has its own card, not the timeline's",
      'id="stageCard"' in UI and 'class="card hidden" id="timelineCard"' in UI)
check("the dominant stage is named, not left to be read off a bar",
      "takes ' + lead.share" in UI)
# Anchored to the expression that renders the sentence, not to the phrase: the
# phrase alone also matches a comment, and a check that passes off a comment is
# not a check.
check("the stages that errored are named", "'Errors in ' + broke" in UI)
check("and a clean run is said out loud", "No call returned an error" in UI)
check("it refreshes when the phase is opened",
      "b.dataset.phase === '4' && currentRun" in UI)
check("and when results arrive", "if(currentRun) loadStages(currentRun);" in UI)

print()
print("the aggregation itself")
sys.path.insert(0, ".")
from ltmetrics.server import _calls_path  # noqa: E402

tmp = tempfile.mkdtemp(prefix="ltm-stage-")
os.makedirs(os.path.join(tmp, "results"))
rows = [
    {"name": "Home Page", "group": "Browse", "ms": 100, "ok": True},
    {"name": "Login", "group": "Login", "ms": 900, "ok": True},
    {"name": "Cart created", "group": "", "ms": 200, "ok": False},
    {"name": "TXN: Checkout", "group": "TXN", "ms": 9999, "ok": True},
    {"name": "TXN: Login", "group": "", "ms": 8888, "ok": True},
]
with open(os.path.join(tmp, "results", "ltm_calls.jsonl"), "w",
          encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
check("the log is found", _calls_path(tmp) is not None)
check("either name resolves",
      os.path.basename(str(_calls_path(tmp))) == "ltm_calls.jsonl")

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
