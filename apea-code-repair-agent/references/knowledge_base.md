# Known bugs & deterministic repair patterns

This is the "KB-first" catalog referenced by SKILL.md. Check here before reasoning a fix out from scratch — if a defect matches an entry below, apply its deterministic fix directly (auto-heal) rather than re-deriving it. This is faster, more consistent across repairs, and is what keeps the repair agent from reinventing the same fix on every single script.

If you fix something that genuinely isn't cataloged here, add a new entry (see the template at the bottom) before finishing the repair — that's how this file (and the repair agent's judgment) improves over time instead of starting from zero on every run.

## KB-001 — Recorded/hardcoded token reused across sessions
**Symptom:** a module-level constant holds a customer/JWT token; the actual login response is discarded or ignored.
**Root cause:** the generator replayed a token captured during recording instead of using the live one from this run's own login call.
**Deterministic fix:** extract the token from the login response immediately after a successful login, store it in a per-user correlation cache (never at module scope), and use it for every subsequent `/rest/` call.
**Maps to:** Phase 5 (Correlation), Phase 6 (Authentication).

## KB-002 — Magento token endpoint returns a bare JSON string
**Symptom:** a `TypeError` (or a "fix" that still indexes into the response) around `response.json()["token"]` or similar.
**Root cause:** Magento's `/rest/V1/integration/customer/token` returns the token as a plain JSON string body, not a wrapped object.
**Deterministic fix:** `token = response.json()` directly — no key lookup.
**Maps to:** Phase 6.

## KB-003 — `regionId` submitted as a string code
**Symptom:** shipping estimate/shipping-information calls send `"regionId": "UKM"` (or any non-numeric value).
**Root cause:** the recorder captured a region *code*, not Magento's required integer region ID.
**Deterministic fix:** resolve country → region → integer ID via `GET /rest/V1/directory/countries/{country_id}` (see the region resolver in `code_patterns.md`) and submit that integer.
**Maps to:** Phase 10.

## KB-004 — Quote/cart ID mismatch between add-to-cart and shipping/payment
**Symptom:** add-to-cart posts to a hardcoded or different quote/cart id than the one shipping/payment operate on (often a literal ID vs. `mine`).
**Root cause:** the recording merged two different session snapshots that don't actually share a cart.
**Deterministic fix:** standardize every cart operation on `/rest/V1/carts/mine/*`, resolved fresh each run.
**Maps to:** Phase 8 (Cart), Phase 9 (Quote consistency).

## KB-005 — Hardcoded shipping/payment method codes
**Symptom:** a literal method code (e.g. `"flatrate"`, `"checkmo"`) with no call to retrieve or validate available methods.
**Root cause:** the recorder captured whichever method happened to be selected in one session; it isn't guaranteed to be offered or enabled elsewhere.
**Deterministic fix:** retrieve the live list of available methods, validate the preferred code is present, and fall back to the first available (shipping) or a known offline method (payment) if not.
**Maps to:** Phase 11 (Shipping), Phase 12 (Payment).

## KB-006 — Order response fetched but never actually validated
**Symptom:** the order-placement response is parsed inside a `try/except` that logs and continues on failure (e.g. `print("order failed but continuing anyway")`), or the response body is never inspected at all.
**Root cause:** sequential-replay generation has no fail-fast concept — it fires the next request regardless of what the last one returned.
**Deterministic fix:** check the response status and parse a real order/increment ID; raise a hard failure if either check fails.
**Maps to:** Phase 13 (Order).

## KB-007 — Unbounded retry loop on 401 (or any status)
**Symptom:** a `while resp.status_code == 401: ...` (or similar) with no attempt cap.
**Root cause:** the generator modeled "keep trying" as recovery instead of "try once more after fixing the cause."
**Deterministic fix:** bounded retry-once wrapper — one recovery action (e.g. token refresh), one retry, then a real failure (see `code_patterns.md`).
**Maps to:** Phase 15 (Error recovery).

## KB-008 — Global mutable state shared across simulated users
**Symptom:** module-level variables (e.g. `last_quote_id`, `retry_count`, a shared token constant) read and written by every `HttpUser` instance.
**Root cause:** the generator didn't model per-user session isolation, so concurrent Locust greenlets corrupt each other's state under load.
**Deterministic fix:** move all per-session values into an instance-level correlation cache created in `on_start`; nothing session-specific stays at module scope.
**Maps to:** Phase 5 (Correlation), Phase 17 (Refactoring).

## KB-009 — Dead/unreachable parameterization branch
**Symptom:** `if isinstance(body, dict): ...` (or similar type-gated injection) guarding logic that never runs because the body is actually a string at that point.
**Root cause:** the generator assumed a body shape that doesn't match what it actually emits.
**Deterministic fix:** normalize the body into a structured model first (Phase 3), then parameterize the normalized structure — never gate on a type check against raw recorder output.
**Maps to:** Phase 4 (Parameterization).

## KB-010 — `print()` instead of structured logging
**Symptom:** scattered `print(...)` calls with no log level, timestamp, or checkout-state context.
**Root cause:** generation shortcut; not a functional bug on its own, but it makes every other bug harder to diagnose after the fact.
**Deterministic fix:** structured logger + a `log_checkout_state`-style helper capturing state, quote ID, cart items, shipping, payment, order ID, elapsed time (see `code_patterns.md`).
**Maps to:** Phase 16 (Logging).

## KB-011 — Missing `catch_response()` / generic or missing request names
**Symptom:** the load test reports 100% success even when business-logic validation inside the task clearly failed; Locust's stats show one row per unique URL/ID instead of grouped endpoints.
**Root cause:** the generator never wired failed validations back into Locust's own request-reporting mechanism, and never grouped parameterized URLs under a stable name.
**Deterministic fix:** wrap every request in `catch_response=True`, call `.success()`/`.failure(...)` explicitly based on the actual validation result, and pass a stable `name=` for any URL containing a variable path segment (IDs, SKUs, etc.).
**Maps to:** Phase 2 (Locust mechanics).

## KB-012 — Blocking `time.sleep()` inside a task
**Symptom:** `time.sleep(n)` called anywhere inside `@task` methods.
**Root cause:** the generator modeled "pause between steps" as a blocking sleep instead of using Locust's own wait mechanism.
**Deterministic fix:** remove the blocking sleep; rely on `wait_time`/`between()` at the `HttpUser` level for pacing between task invocations.
**Maps to:** Phase 2 (Locust mechanics).

---

## Adding a new entry

When you repair something that doesn't match any pattern above, add a new `KB-0NN` entry here (same Symptom / Root cause / Deterministic fix / Maps to shape) before finishing the repair. This is the mechanism that lets the next repair on a similar script be an auto-heal instead of a from-scratch investigation — treat it as part of finishing the job, not an optional extra.
