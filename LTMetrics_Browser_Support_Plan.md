# LT Metrics — First-Class Browser Interaction Support
## Implementation Plan (Hybrid execution model)

**Status:** design / sign-off. No pipeline code changed yet.
**Chosen model:** Hybrid — HTTP/Locust for bulk load; a small real-browser (Playwright) pool for the payment / auth-iframe portion.
**Constraint that shapes everything:** Locust has no browser. Frames, Shadow DOM, tabs/windows, OAuth screens and 3DS challenges are *detected and correlated* at HTTP level, but only *executed* on the browser track. Nothing browser-only is faked into a Locust script.

---

## 1. The hybrid principle (how "seamless" actually works)

One run produces **two tracks** that share config and merge into one report:

- **Track A — HTTP (Locust), full scale (N VUs).** The existing pipeline. Exercises the API journey (browse, search, cart, shipping, and offline-payment order). All dynamic tokens that travel over HTTP are correlated. Where the recorded checkout hits a hosted payment iframe / 3DS / OAuth screen, Track A either uses an offline method (as today) or stops cleanly at the browser boundary — it never fakes frame handling.
- **Track B — Browser (Playwright), small pool (M VUs, M ≪ N).** A generated browser runner drives the *complete* end-to-end journey including the payment iframe, Shadow DOM fields, 3DS/OAuth. Its job is to prove the browser-only path works under some concurrency and to measure real gateway/auth latency. ~1 browser per VU, so M is tens, not thousands.

The **Browser Context Graph** (new shared artifact, produced by Step 01) is what lets the orchestrator classify each step as *HTTP-replayable* vs *browser-only*, generate both tracks, and route execution. Tracks are independent (each logs in from CSV) — no fragile cross-process session handoff.

Concurrency targets: Track A unchanged (100s–1000s VUs). Track B bounded (default 5–25 browser VUs, configurable), reported separately and merged.

---

## 2. Component map (what changes, where)

| Layer / file | Change | New or Δ |
|---|---|---|
| `ltmetrics/agents/browser_context.py` **(new)** | Detect iframes/nested frames/Shadow DOM/windows/OAuth/3DS/cross-domain from recording + Selenium actions; emit **Browser Context Graph** | new |
| `ltmetrics/agents/parser.py` + `recording.py` facade | Call browser_context on parse; attach `browser_context` to result; keep `static_dropped`/`group` behavior | Δ |
| `ltmetrics/agents/parameterization.py` | Add browser-token correlations: payment_intent, client_secret, JWT, OAuth `state`, frame/window ids, dynamic browser context | Δ |
| `ltmetrics/agents/generator.py` | Emit **step classification** (http vs browser); Locust track unchanged for HTTP; new **Track-B** browser runner generation; correlation rules for new tokens | Δ + new template |
| `ltmetrics/agents/engines/playwright_engine.py` **(new)** | Engine implementing the existing `base.py` interface; launches the browser runner, reports stats | new |
| `ltmetrics/agents/engines/__init__.py` (`get_engine`) | Register `playwright`; add `hybrid` router | Δ |
| `ltmetrics/agents/executor.py` | Hybrid mode: launch Locust **and** Playwright runner, monitor both, merge stats into one run state/report | Δ |
| `ltmetrics/agents/validation.py` | Validate frame hierarchy / Shadow DOM / cross-domain / token correlation / browser-context consistency against the graph | Δ |
| `ltmetrics/agents/repair.py` | KB-first repair for iframe/nested-frame/Shadow DOM/popup/OAuth/gateway/browser-context changes | Δ |
| `ltmetrics/knowledge/kb.py` + `rules/browser_patterns.yaml` **(new)** | iframe patterns; Stripe/Adyen/PayPal/Cybersource/Braintree; Shadow DOM; OAuth; 3DS patterns | Δ + new file |
| `ltmetrics/memory.py` + `ltmetrics/db.py` | Store learned browser context, payment-flow patterns, window/frame relationships, platform browser behaviors | Δ |
| `ltmetrics/platforms.py` | Capability flags: iframe, shadow_dom, popup, oauth, websocket, graphql | Δ |
| `ltmetrics/agents/orchestrator.py` | Route per-segment (HTTP vs browser); decide track mix; consult graph + registry | Δ |
| `ltmetrics/agents/analyzer.py` | Classify iframe/frame-detach/Shadow DOM/popup/gateway/OAuth/browser-context failures in RCA | Δ |
| `ltmetrics/server.py`, `ltmetrics/cli.py`, `static/index.html` | Surface graph, browser-track controls (M VUs), and browser failures | Δ |

---

## 3. Browser Context Graph — data schema

Produced by Step 01, persisted to `results/browser_context.json`, attached to the parse result and to `discovery["browser_context"]`, and saved into `discovery.json` (so saved scripts keep it). Shape:

```json
{
  "windows":  [{"id":"w0","opener":null,"url_pattern":"/checkout"}],
  "frames":   [{"id":"f_stripe","parent":"w0","url_pattern":"js.stripe.com/.../elements",
                "gateway":"stripe","role":"payment"}],
  "nested_frames": [{"id":"f_3ds","parent":"f_stripe","url_pattern":"3ds","role":"3ds"}],
  "shadow_dom": [{"host_selector":"#card-element","gateway":"stripe"}],
  "popups":   [{"trigger":"paypal-button","url_pattern":"paypal.com/checkoutnow","role":"oauth"}],
  "cross_domain": [{"from":"storefront-uat...","to":"scholarfltest.b2clogin.com","role":"oauth"},
                   {"from":"...","to":"js.stripe.com","role":"payment"}],
  "oauth":    {"present":true,"provider":"azure_b2c","state_param":"state","redirect":"..."},
  "threeds":  {"present":true,"style":"redirect|iframe|popup"},
  "storage":  {"localStorage_keys":["..."],"sessionStorage_keys":["..."],"cookies":["..."]},
  "steps":    [{"idx":7,"label":"Enter card","execution":"browser","frame":"f_stripe"},
               {"idx":3,"label":"add to cart","execution":"http"}]
}
```

Detection sources (deterministic first, LLM to fill gaps): recorded Selenium action lines (`switchToFrame`, `switchToWindow`, selectors with `#shadow-root`, `frameLocator`), request hosts (js.stripe.com, checkout.stripe.com, *.adyen.com, paypal.com, *.b2clogin.com, cybersource, braintreegateway), redirect chains (`state=`, `code=`, `3ds`, `acs`, `PaReq`), and `window.open`/target=_blank markers.

**`steps[].execution` is the key field** — `http` steps go to Track A, `browser` steps go to Track B. This is the single source of truth every downstream layer reads.

---

## 4. Phase plan (order, each independently shippable & testable)

- **P0 — Foundation (no behavior change to runs).** `browser_context.py` + graph schema + wiring into parser/facade + persist to `browser_context.json` and `discovery.json`. Feed the graph read-only into parameterization/generator/validation/repair/analyzer (they accept it but don't yet act). Add `test_browser_context.py`. *Risk: low (additive).*
- **P1 — Detection depth.** Deterministic detectors for iframes/nested/Shadow DOM/windows/OAuth/3DS/cross-domain; LLM enrichment in `metadata_generator`. Surface counts in analyze response + UI (like `static_dropped`).
- **P2 — Correlation.** `parameterization.py` adds extractors/injectors for payment_intent, client_secret, JWT, OAuth `state`, frame/window ids. These are real HTTP-token correlations that also benefit Track A.
- **P3 — Generator: classification + Track A.** Generator reads `steps[].execution`, keeps the Locust script to HTTP steps, and emits a manifest of browser-only steps. No fake frame code in Locust. Track A behaves as today for the HTTP portion.
- **P4 — KB / Memory / Platform Registry.** `browser_patterns.yaml` (gateway iframe/field selectors, Shadow DOM hosts, OAuth/3DS patterns); memory fields; registry capability flags. Repair + orchestrator consult them.
- **P5 — Track B: Playwright engine (the big one).** Generate `playwright_runner.py` from the graph + KB gateway patterns; `engines/playwright_engine.py`; executor hybrid launch (Locust + Playwright), merged stats; server/UI control for browser-VU count. *Risk: high, resource-heavy, needs Playwright + browsers installed; can't be verified with the sandbox down.*
- **P6 — Validation / Analyzer / Repair for browser.** Validate graph consistency; RCA classifies browser failure classes; repair maps gateway/selector drift to KB fixes and regenerates the browser runner.
- **P7 — UI.** Show the graph (frames/windows tree), the browser-track KPIs, and browser failures in the live view + report.

Recommended stop-and-verify points: after **P0** (foundation is safe and useful even alone), after **P3** (HTTP correlation gains, still no browser dependency), and after **P5** (the browser track — verify on a machine with Playwright before trusting).

---

## 5. Hybrid execution — the mechanics (P5 detail)

- `engines/playwright_engine.py` implements the same interface as `locust_engine.py` (`single_cmd`/`master_cmd`/`available`), but launches `python playwright_runner.py --vus M --duration D` and writes `results/playwright_stats.csv` + appends to the same `ltm_calls.jsonl` (so the live API-calls feed shows browser steps too).
- `executor.run_blocking` gains a **hybrid branch**: start the Locust master (Track A, N VUs) and the Playwright runner (Track B, M VUs) together; `_monitor` reads both stat files; the report shows Track A (HTTP throughput) and Track B (browser/payment latency, order success) side by side.
- `playwright_runner.py` (generated): a fixed VU pool, each context loops the journey using the graph — `page.frame_locator(...)` for payment iframes, `.locator("#host").shadow_root`-style traversal via KB selectors, `context.on("page")` for popups/OAuth, wait-for-navigation for 3DS/redirects. Credentials/data from the same CSV. Fails fast and records per-step timing + order outcome.
- No session handoff between tracks — each track logs in independently. This keeps it robust and avoids the fragile "pause Locust VU, borrow a browser, resume" pattern.

---

## 6. Risks & mitigations

- **Fundamental:** browser-only steps cannot run on Track A. *Mitigation:* explicit `execution` classification; Track A flags them, Track B runs them. No fake code — the earlier "don't hallucinate completion" rule holds.
- **Resource cost:** Track B is ~1 browser/VU (hundreds of MB each). *Mitigation:* small default pool (5–25), hard cap, documented; it measures the payment path, it is not the load driver.
- **Dependency:** Playwright + browser binaries must be installed on the runner. *Mitigation:* `playwright_engine.available()` gates it; if absent, LT Metrics runs Track A only and clearly says Track B was skipped (mirrors today's crawl fallback).
- **Third-party gateways:** driving Stripe/Adyen/PayPal at load may violate ToS and needs sandbox/test mode. *Mitigation:* Track B targets gateway **sandbox/test** endpoints only; documented; never real card processing.
- **Regression to the working pipeline:** most changes are additive, but generator/executor/parameterization are on the critical path. *Mitigation:* graph is read-only until P3; `execution="http"` default when no graph → today's behavior exactly; each phase behind the presence of a graph.
- **Verification gap (sandbox down all session):** I cannot run any of this here. *Mitigation:* every phase ships with a static-analysis pass + a `test_*.py`, and P5 explicitly must be run on your machine (with Playwright) before trust. This plan assumes you verify at the stop points above.

---

## 7. Verification strategy (given no sandbox here)

- `test_browser_context.py` — parse the Radwell + Amneal + a Stripe/3DS sample; assert the graph detects frames/windows/oauth/3ds and classifies steps.
- Extend `test_recording_parity.py` — assert P0 is behavior-preserving (flow/groups/static unchanged when no browser context present).
- P2 correlation unit checks (token extract/inject regexes).
- P5 — a real run on a Playwright-equipped machine; assert an order completes on Track B through the payment iframe in gateway sandbox mode.

---

## 8. Open decisions for you

1. **Track-B default size** (browser VUs): propose 10, hard cap 50.
2. **Gateways to seed first** in `browser_patterns.yaml`: propose Stripe + Adyen + PayPal (your SUFS/Radwell stack — confirm Cybersource/Braintree priority).
3. **Where Track B targets** for payments: confirm gateway **sandbox** credentials exist for the test env (required — we will not drive live payments).
4. **Playwright availability** on your load runner / CI: confirm it can install browsers, else Track B stays local-only.

On sign-off I'll start at **P0** (safe, additive foundation) and stop for your verification before each subsequent phase.
