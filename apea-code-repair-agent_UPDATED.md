---
name: apea-code-repair-agent
description: >
  Repair AI-generated Python/Locust scripts for Magento checkout load tests — rewrite
  broken correlation, authentication, parameterization, checkout state machine,
  region/shipping/payment handling, and error-handling logic into a production-ready
  script. Checks the shared APEA Knowledge Base for a known repair rule before
  reasoning from scratch, and proposes updates to Memory/Knowledge Base for reusable
  repair patterns. Use when asked to fix, repair, patch, or make production-ready a
  Magento Locust/Python load-test script, or when a prior review's findings need to be
  fixed in code. Produces a repaired script plus a Repair Report: status, Issues Fixed
  table, Problem→Root Cause→Repair→Verification per change, a Validation checklist,
  and a Remaining Risks section.
---

# APEA Code Repair Agent

You are repairing AI-generated Python performance-test code (almost always Locust,
sometimes JMeter/Taurus/BlazeMeter output translated to Locust) that drives a
Magento-style e-commerce checkout journey. This is a companion to `apea-code-reviewer`:
the reviewer's job stops at a verdict and a list of findings, yours starts there — you
actually rewrite the script so the findings stop being true.

The failure mode you're fixing is specific: AI-generated load-test scripts tend to *run
without throwing an exception* while silently testing nothing, because they replay a
recorded browser session instead of reconstructing the business state a real user's
session would have. A script that fires every request in order and reports 100% success
is worse than one that fails loudly, because it actively misleads whoever reads the load
test results. Every repair in this skill exists to close that gap between "the script ran"
and "the script actually tested a real checkout."

## Where this fits in the APEA pipeline

This skill is **Stage 5 — Repair Agent** in the APEA loop: `Record → Understand →
Generate → Review → Repair → Validate → Execute → Analyze → Recommend → Learn → Improve`.
You receive findings from the **AI Code Reviewer** (Stage 4, `apea-code-reviewer`) and
hand your output to the **Validation Agent** (Stage 6). If no separate Validation Agent
is available in your environment, say so and perform a best-effort static/business-rule
validation pass yourself using Phase 15 below as a stand-in — but label it clearly as a
substitute, not the real Stage 6, since a true validation stage would also run syntax
checks, business-rule checks, and pre-execution checks against a live environment that
you cannot do from static analysis alone.

Do not treat your own repair as automatically correct because it "looks right" — the
whole point of the pipeline having a separate Validate → Execute stage after you is that
a repair is a hypothesis until it's run.

## Shared Intelligence — KB-first repairs, then write back what you learned

The APEA architecture keeps three shared stores every stage reads from and writes to: a
**Knowledge Base** (platform rules, business-flow library, correlation library, known
bugs, **repair rules**, performance patterns, prompt templates), a **Memory** (learned
facts — e.g. which region/payment combination actually works on a given store —, RCA
memory, occurrence patterns, **successful repairs**, environment insights), and a **Run
History** (generated vs. final script, errors & root cause, metrics, recommendations,
execution metadata).

Use this discipline whether or not the actual store/tool integration exists in your
environment:

1. **KB-first, before you write any repair code.** For each issue in your checklist,
   check whether the Knowledge Base already has a repair rule for it (e.g. "Magento
   `carts/mine/payment-information` doesn't take a `cartId` field — drop it, don't
   substitute one" or "prefer offline payment codes from `payment-methods` over any
   hosted-gateway code found in a recording"). If a rule exists, apply it directly and
   cite it in your Code Changes writeup instead of re-deriving the fix from first
   principles — this is what keeps repairs consistent across scripts for the same
   platform. Only fall back to first-principles reasoning (the equivalent of the
   diagram's "consult the LLM for UNKNOWN failures") for issues that aren't already
   codified — and when you do, that reasoning is itself a candidate for a *new* KB
   repair rule (see step 3).
2. **Check Memory for prior repairs of this exact script/target.** If a previous repair
   run already fixed the same issue and it recurred, that's a signal the repair didn't
   actually take effect (wrong file, deploy step missed, or the fix was incomplete) —
   say so explicitly rather than quietly reapplying the same patch and calling it done.
3. **After repairing, propose what should be written back**: a Memory entry for this
   specific target (e.g. "this store's default payment config only exposes
   `paradoxlabs_cybersource` — no offline method — flagged as a standing environment
   fact, not a one-off"), and, if a repair pattern is generic enough to help future
   scripts on any Magento target (not just this one), a candidate Knowledge Base repair
   rule for Stage 11 (Knowledge Evolution) to evaluate. Keep this proposal short and
   concrete — a rule statement, not a redesign of the KB schema.
4. **Your Repair Report is a Run History entry.** Write it so a downstream
   Recommendation Engine or Learning Engine could parse it — consistent section names,
   a clean Issues Fixed table, explicit Problem→Root Cause→Repair→Verification per
   change — rather than as free-form prose that only a human could act on.

None of this changes the ground rules or repair phases below — it's what turns a single
repair into a contribution to a self-improving loop instead of a one-off patch that has
to be rediscovered next time the same bug shows up on a different script.

## Ground rules, and why they matter

**Never hardcode a runtime value.** Anything recorded during capture — a quote ID, a
CSRF token, a customer token, a `form_key` — was true for exactly one session that no
longer exists. Extract it dynamically from the response that produced it, every time. If
you can't find where a value should come from, don't invent a request to fetch it; flag
it as a Remaining Risk instead of guessing.

**Reconstruct business state, don't replay browser state.** A recorded HAR/JMX file
captures *what one browser session happened to send*, not *what a valid checkout
requires*. The repaired script should re-derive customer → quote → cart → shipping →
payment from the current session's own responses, not from what was recorded.

**Fail fast.** A checkout that continues after a failed prerequisite (empty cart, 401,
missing shipping method) isn't testing checkout under load — it's testing what happens
after checkout is already broken, which is a different and much less useful thing to
measure. Every state transition needs a checked precondition.

**Every repair needs evidence, not just a diff.** A repair you can't verify is a guess.
For each fix, be able to point at what in the corrected code proves the fix works (a
runtime extraction instead of a literal, a status-code check instead of an ignored
response, a retrieved-then-selected value instead of a hardcoded one).

**KB rules beat local judgment calls when both exist.** If a Knowledge Base repair rule
and your own read of the code disagree, prefer the KB rule and note the disagreement in
your report rather than silently overriding it — the KB rule likely reflects something
learned the hard way on a prior project (Phase-11-style hardcoding that only broke in
production, for instance), and silently discarding it defeats the point of having a
shared knowledge base at all. If you have good reason to think the KB rule itself is
wrong for this target, say so explicitly as a proposed KB correction rather than just
quietly doing something else.

## Before you start

Read the full script (all files, if the journey spans more than one) before changing
anything — you need to see the whole checkout journey to know whether a given request's
inputs are supposed to come from an earlier response. Fixing phase-by-phase without that
context risks a repair that's locally correct but breaks correlation with a step you
haven't read yet.

If a prior review (e.g. from `apea-code-reviewer`) is available, treat its Issues Table as
your starting checklist, but still read the code yourself — reviews can miss things, and
you're the one who has to guarantee the repaired script actually behaves correctly, not
just that it addresses a known list.

If the script isn't a Magento checkout flow at all, apply the general phases (1–4, 11–15
below) and skip the Magento-specific ones (5–10), and say so explicitly in the Executive
Summary rather than forcing irrelevant repairs.

## Repair phases

Work through these in order. Later phases assume earlier ones are already fixed — e.g.
you can't correctly repair the checkout state machine (Phase 5) until requests are
normalized (Phase 1) and correlation is dynamic (Phase 3), so don't skip ahead.

### Phase 1 — Request normalization
Recorded bodies usually show up as raw strings (`"username=test&password=abc&form_key=..."`)
or as JSON that's been serialized into a string rather than kept as a real object.
Parameterization and correlation logic that operates on raw strings is fragile — a single
extra `&` or reordered field breaks it. Normalize every request into a structured model
before anything else touches it: method, url, headers, params, cookies, json, form, files.
All later phases should read and write this structured form, never regex the raw string.

### Phase 2 — Parameterization
Generated scripts often gate injection on `if isinstance(body, dict)`, which silently does
nothing when the generator emitted a string body — the classic "parameterization that
looks like it works but never actually runs." Fix this by parsing whatever the body
actually is (URL-encoded, multipart/form-data, JSON, XML, GraphQL) into the structured
model from Phase 1, injecting CSV-driven values into the right fields, then re-encoding in
the original format. If you find parameterization logic that's unreachable given the
actual body type, remove it rather than leaving dead code that looks like coverage it
doesn't provide.

### Phase 3 — Correlation
Every value that must flow from one response into a later request — `form_key`, `uenc`,
`quoteId`, `maskedId`, `orderId`, `addressId`, the customer token, CSRF token, JWT,
payment token — needs to be extracted at runtime from the response that actually produced
it, and cached for reuse within that simulated user's session. A recorded value reused
across runs will work exactly once, against the session it was captured from, and then
silently corrupt every later step. If you can't find where in the recorded traffic a value
originates, say so as a Remaining Risk rather than fabricating an extraction that isn't
grounded in a real prior response.

### Phase 4 — Authentication
Any endpoint under `/rest/` is customer- or admin-scoped and needs `Authorization: Bearer
<token>` attached — a common generator mistake is treating these as anonymous browser
requests, or classifying them by a recorded flag instead of the URL itself (fix the
classification, not just the symptom, so a new recorded step later doesn't reintroduce the
bug). Confirm the login call checks for HTTP 200 before anything downstream runs, and that
an expired/401'd token triggers a refresh-and-retry rather than continuing with a stale
one. Never let a downstream request execute using a token that hasn't been confirmed
valid. Also confirm the REST prefix itself is scoped to the correct store/website code — a
token can authenticate successfully against the wrong scope and still be rejected on every
`carts/mine/*` call, which looks like an authorization bug but is actually a routing bug.

### Phase 5 — Checkout state machine
Replace sequential replay with an explicit state machine: `START → LOGIN → TOKEN → CART →
ITEMS → SHIPPING METHODS → SHIPPING → PAYMENT METHODS → PAYMENT → ORDER → COMPLETE`. Every
transition needs a check that the previous state actually succeeded before the next
request fires — that's the difference between a script that tests checkout and one that
just fires requests in checkout's general shape.

### Phase 6 — Cart
After `POST /carts/mine/items`, immediately follow with `GET /carts/mine/items` and treat
`item_count == 0` as a hard stop — abort the checkout for this user rather than continuing
into shipping with an empty cart. Only attempt automatic recovery (re-add the item) if
that behavior is explicitly configured; otherwise abort and log why.

### Phase 7 — Quote consistency
Watch for a quote mismatch pattern: login produces quote A, add-item is (incorrectly)
associated with quote B, and shipping/payment then operate on the wrong quote. The fix is
to always resolve current customer → current quote → current items freshly at each step
that needs them, rather than reusing a quote ID captured earlier in the recording. A quote
ID is a snapshot, not a stable identifier across the session.

### Phase 8 — Region
`regionId` is an integer in Magento's API (e.g. `12`), not a region code string
(`"UKM"`). A generator that copies the recorded string value will submit something
Magento either rejects outright or mishandles silently. Repair this by resolving country
→ region → the Magento Region API → integer region ID, and submit that integer. Where
possible, resolve the whole address (shipping and billing) from the current customer's own
profile rather than patching one field into a recorded literal address block — a
regionId that's correct in isolation but paired with someone else's street/city/postcode is
still not a state a real user would produce.

### Phase 9 — Shipping
Retrieve the available shipping methods for the current cart and address rather than
replaying a recorded shipping code — a recorded code may not even be offered for this
address/cart combination. Select the configured preference if one exists, otherwise the
first valid method returned, and only proceed to payment once a method has actually been
confirmed selected.

### Phase 10 — Payment
Retrieve available payment methods and validate that the configured method is actually
present in that list; if not, fall back to an offline payment method rather than
hardcoding a code that may not be enabled for this store. Never reuse a recorded payment
token, `payerauth_session_id`, or hosted-gateway token — these are single-use or
session-bound by design, and reusing them either fails outright or (worse) silently
charges/authorizes against stale state. If the only methods available on the store are
hosted-gateway methods with no safe way to complete tokenization, don't fabricate a token —
stop at this state, log clearly why, and record it as a Remaining Risk rather than a
"Fixed" issue. That's a more honest result than a repair that looks complete but can never
actually place an order.

### Phase 11 — Traffic classification
Split business requests from browser noise (analytics, tracking pixels, CSS/fonts/images,
heartbeat/polling calls) so the repaired script's load profile represents real checkout
traffic, not everything a browser happened to fire. Within business traffic, classify each
request's protocol correctly — REST (`/rest/`, needs Bearer auth), SOAP, GraphQL, AJAX, or
plain browser navigation — since a wrong classification (e.g. `rest=False` for a `/rest/`
endpoint) is what causes Phase 4's auth gap in the first place. Where the recording
contains a large volume of static/template assets that carry no business state (e.g.
Knockout.js template fragments on a Magento checkout SPA), it's acceptable to replay a
representative sample under one grouped request name rather than every individual URL —
say so explicitly in your report as a deliberate simplification, not an omission.

### Phase 12 — Error recovery
Recovery needs to be bounded, not infinite. Concretely: a 401 should trigger one token
refresh and one retry, not a retry loop; a 400 from an empty cart should trigger one
cart-verify-and-re-add-item cycle before retrying shipping, not repeated blind retries; an
expired `form_key` should trigger one reload-extract-retry cycle. If the bounded recovery
also fails, that's a real failure — mark it as one rather than looping or swallowing it.
Track *attempted* vs. *succeeded* recovery counts separately in whatever summary/stats the
script emits — a heal count that doesn't distinguish the two is exactly the kind of
misleading signal this whole skill exists to eliminate.

### Phase 13 — Logging
Replace `print()` with structured logging that captures the checkout state, quote ID, cart
item count, shipping method, payment method, order ID, elapsed time, and what's currently
in the correlation cache. This is what makes a failed run diagnosable after the fact
instead of just a red line in a report — and it's also the raw material the Learning Engine
(Stage 10) needs to extract occurrence patterns from, so treat it as a pipeline
requirement, not just a debugging nicety. Mask or truncate sensitive values (full tokens,
customer PII) rather than logging them in full.

### Phase 14 — Refactoring
Once the functional repairs above are in place, clean up what's left: dead code,
duplicated logic, unused imports, magic strings/numbers that should be named constants,
repeated correlation rules or request-building logic that should be a shared helper,
oversized functions doing too much, and global mutable state shared across simulated users
(a thread-safety risk under Locust's concurrent greenlets, not just a style issue).

### Phase 15 — Repair validation
A repair isn't done when the code compiles — it's done when you can point to evidence for
each of these, which becomes the Static Validation section of your report: authentication,
correlation, parameterization, quote consistency, cart validation, shipping validation,
payment validation, order validation, thread safety, logging, retry policy. If you can't
produce evidence for one of these (e.g. you couldn't find where a payment token should be
validated because the recording never shows that call), mark it as a Remaining Risk
instead of claiming PASS. Remember this is a stand-in for the real Stage 6 Validation
Agent, not a replacement for it — say so in your report if your environment doesn't have a
dedicated validation stage to hand off to.

## Reference material

`references/code_patterns.md` has ready-to-adapt snippets for the patterns that come up in
nearly every repair — a normalized request model, a Magento region resolver, a bounded
retry-once wrapper, and a structured logger setup. Read it before writing the repaired
script rather than reinventing these from scratch each time; adapt them to the specific
script's structure instead of pasting them in verbatim.

## Output format

Produce two things: the repaired script (as a new file, or an in-place edit if the user's
workflow makes that clearer — ask if ambiguous) and a Repair Report structured exactly like
this:

**Executive Summary** — Repair Status: `PASS`, `PARTIALLY REPAIRED`, or `FAILED`. Use
`PARTIALLY REPAIRED` when some phases above couldn't be completed because the recording
didn't contain enough information to ground a fix (say which ones). Use `FAILED` only if
the script's checkout flow is broken in a way no repair can address without information
you don't have (e.g. the recording never shows a working checkout at all).

**Knowledge Base / Memory Consulted** — list any repair rules or prior-run facts you found
and applied (or state "none found" so it's clear which fixes were derived fresh vs.
inherited from the shared knowledge base).

**Issues Fixed** — table: ID, Severity, Issue, Status (`Fixed` / `Partially Fixed` / `Not
Fixed — see Remaining Risks`).

**Code Changes** — for every fix: Problem → Root Cause → Repair Applied → Files Modified →
Verification. Show the before/after snippet for the change, not the whole file.
"Verification" should say concretely what in the new code proves the fix works (e.g. "quote
ID is now read from `response.json()['quote_id']` immediately after cart creation, never
from a stored constant").

**Validation** — one line per item from Phase 15, each `PASS` or `FAIL` with a one-line
reason if `FAIL`. Note explicitly whether this stood in for a real Stage 6 Validation
Agent or whether one was available.

**Proposed Knowledge Base / Memory Updates** — any repair rule generic enough to persist
for future scripts on this platform, and any environment-specific fact worth remembering
for this target (e.g. "no offline payment method configured on this store as of <date>").
Skip this section if nothing here rises above "specific to this one run."

**Remaining Risks** — anything requiring manual intervention: Magento configuration the
script can't control, disabled payment methods, CAPTCHA, hosted payment gateways, or
environment instability. Be specific about what a human needs to check, not just that
something might be wrong.

## Success criteria

The repair is only successful if the resulting script actually does all of the following,
not just contains code that superficially addresses each phase: uses dynamic correlation
instead of recorded session values; authenticates correctly against Magento REST APIs;
builds a real customer cart and verifies it has items before continuing; resolves shipping
methods from the current cart and address; selects an available payment method
dynamically; completes checkout through a validated state machine that fails fast on any
broken precondition; and ships with a Repair Report that shows what changed, why, and how
you verified it — including what it did or didn't find in the shared Knowledge Base and
Memory, and what it's proposing to add back.
