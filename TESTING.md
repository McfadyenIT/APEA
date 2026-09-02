# Testing the latest changes — Orchestrator + Knowledge Base + Memory

This guide verifies the three new layers added to LT Metrics:

- **Knowledge Base** (`ltmetrics/knowledge/`) — shared rules every agent consults.
- **Memory** (`ltmetrics/memory.py` + `ltmetrics/db.py`) — each run teaches the next.
- **Orchestrator / Decision Engine** (`ltmetrics/agents/orchestrator.py`) — retry / stop / escalate / invoke-Claude control logic.

There are two test levels. **Run Test A first** (fast, deterministic, no network); it proves the "brains". **Then run Test B** for the live integration proof.

---

## 0. Prerequisites

1. Open a terminal in the project root:
   `C:\Pradish\Performance Testing\Performance Orchestration`
2. Activate the virtual environment:
   ```
   .venv\Scripts\activate.bat
   ```
   (If `.venv` doesn't exist yet, run `start.bat` once — it creates it — then close the app.)
3. Ensure PyYAML is present (used to read the KB rule files):
   ```
   python -c "import yaml; print('pyyaml ok')"
   ```
   If that errors: `pip install pyyaml`.

---

## Test A — Offline unit test (≈30 seconds, no server, no site)

This proves the Knowledge Base, the Memory learn→recall loop, and the Decision
Engine — using the real Radwell error strings — without needing a reachable site
or a completed order.

**Steps**

1. From the activated venv, in the project root, run:
   ```
   python test_knowledge_layer.py
   ```

**Expected result** — every line prints `PASS`, ending with:
```
ALL CHECKS PASSED
```
and the process exits with code 0.

**What each block confirms**

| Block | Confirms |
|-------|----------|
| 1) Knowledge Base loads | rule files parse; region/int rule, correlation library, known bugs and the business-flow model are present |
| 2) Error classification | each real failure (`product not found`, `out of stock`, `payment unavailable`, `empty cart`, `JWT rejected`) maps to the right bug — **no LLM** |
| 3) Repair lookup | each known failure returns the correct deterministic repair action |
| 4) Memory learn→recall | a "Run 143" that failed on `regionId` teaches Memory; `facts_for()` shows "Run 144" would apply `region_policy=omit_region_id_unless_integer` |
| 5) Decision Engine | returns RETRY / STOP / ESCALATE / INVOKE_CLAUDE correctly; payment policy avoids the hosted gateway |

**If a check FAILS:** copy the failing line (it prints what it got vs expected)
and share it. Block 1 failing with "empty" almost always means PyYAML isn't
installed (see Prerequisites).

---

## Test B — Live end-to-end via `start.bat`

This proves the Knowledge Base + Memory are wired into a real run (the learn
loop, and KB-first repair that skips the LLM for known failures).

> Note: a live run needs Radwell staging reachable, and checkout may still stop
> legitimately at out-of-stock / payment (staging config). That's fine — the
> KB/Memory behaviour below is observable regardless of whether an order is placed.

### B1 — Start fresh and run

1. **Close any running LT Metrics** (the console window from a previous `start.bat`),
   so the new code is loaded.
2. Double-click **`start.bat`** (or run it from the terminal). Wait for the
   browser to open at `http://127.0.0.1:8000`.
3. Create/open the Radwell project, upload the recording + CSV, and start a run.
   Let it finish.

### B2 — Verify Memory recorded what the run learned

After the run finishes, query the history DB (project root, `ltmetrics_history.db`):
```
python -c "import sqlite3;c=sqlite3.connect('ltmetrics_history.db');print('FACTS:',c.execute('select scope_key,fact_key,fact_value from learned_facts').fetchall());print('RCA:',c.execute('select error_signature,root_cause,occurrences,resolved from rca_memory').fetchall())"
```
**Expected:** at least one `RCA` row whose `error_signature` matches what the run
hit (e.g. `product_out_of_stock`, `payment_method_unavailable`, `region_id_type`),
and — for recognised failures — a target-scoped `FACTS` row
(e.g. `region_policy = omit_region_id_unless_integer`). If the run created an
order, you'll also see a `last_success_build` fact.

### B3 — Verify KB-first repair (no LLM for known bugs)

Open the newest run's `heal.json`:
```
ltmetrics/projects/radwell.../mcstaging-radwell-eu/<run-id>/heal.json
```
**Expected for a recognised failure:** `reason` reads
*"recognised by knowledge base (no LLM) — deterministic fix '…' is applied by the
generator; re-run to confirm"* and there is a `kb` block naming the matched bug.
This proves the failure was diagnosed from the Knowledge Base **without** an
Anthropic call. (Only genuinely unknown failures fall through to Claude, and that
prompt is now grounded in the KB rules.)

### B4 — Verify the loop remembers across runs

1. Run the **same** Radwell test a **second** time.
2. Re-run the B2 query. **Expected:** the same `error_signature` now shows
   `occurrences = 2` — proof LT Metrics is accumulating knowledge run over run rather
   than rediscovering it each time.

### B5 — Verify the live diagnostics still work

In the run's `results/ltm_flow.json` (or the UI Checkout State card) confirm the
build stamp is today's, the mode is `rest-checkout (validator)`, and the
`checkout_state` / `quote_trace` are populated — i.e. the validator and quote
tracing from earlier fixes are running under the new architecture.

---

## What each test covers

| Layer | Test A (offline) | Test B (`start.bat`) |
|-------|:---:|:---:|
| Knowledge Base (classify + repair lookup) | ✅ | ✅ (via `heal.json`) |
| Memory (learn → recall) | ✅ (synthetic) | ✅ (real DB rows, cross-run) |
| Decision Engine (retry/stop/escalate) | ✅ | ❌ not on the live path yet |
| Generated-script validator / quote trace | — | ✅ |

**Important:** the Orchestrator/Decision Engine class is **not yet routed into the
live `server.py` / `cli.py` run flow** — so `start.bat` does not exercise it. Test A
is what proves that piece today. (Wiring the live run through the Orchestrator is
the planned next step; once done, `start.bat` alone will cover everything.)

---

## Troubleshooting

- **Test A block 1 fails ("empty"):** `pip install pyyaml`, re-run.
- **No `learned_facts` / `rca_memory` tables:** they are created on first use;
  run any LT Metrics run once (or Test A, which calls `db.init_db()`), then re-query.
- **`heal.json` still mentions Claude:** that failure wasn't in the known-bug
  catalogue (unknown failure) — expected. Add its signature to
  `ltmetrics/knowledge/rules/known_bugs.yaml` to make it deterministic next time.
- **KB edits not taking effect:** the KB is cached per process; restart LT Metrics
  (or call `KB.reload()`), then re-run.

## Cleanup (optional)

Test A writes throwaway rows under the host `kb-selftest.example`. To remove them:
```
python -c "import sqlite3;c=sqlite3.connect('ltmetrics_history.db');c.execute(\"delete from learned_facts where scope_key='kb-selftest.example'\");c.commit();print('cleaned')"
```
