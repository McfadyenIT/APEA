# APEA Performance Testing Platform — Capability & Gap Assessment

**Prepared for:** Pradish Kumar, McFadyen Digital
**Date:** August 5, 2026
**Basis:** Static review of the codebase (`apea/`) and stored run artifacts (`projects/…/results/`) during this session. **No live execution** was possible — the sandbox was unavailable — so statements about behaviour at scale are inferred from code and past run logs, not measured here.

---

## Verdict

APEA has the right foundation and an unusually complete pipeline. Today it is best described as a **strong Magento-checkout load-*validation* tool that is reaching toward being a general performance platform** — not yet a hardened, general-purpose perf tool on par with vanilla Locust, k6, JMeter, or Gatling. For McFadyen's Magento and commerce engagements specifically, it is a real asset and, with focused hardening on reproducibility and scale, could be genuinely differentiating. As a generic enterprise perf platform it is not there yet.

The single most important gap for a *measurement* tool is **run-to-run reproducibility**: APEA's self-healing and adaptive behaviour optimise for "get a green run," which is at odds with a perf tool's core job of being a stable ruler you can trend across releases.

---

## What APEA is

A local platform that turns a recording (JMX / HAR / Taurus YAML) into an executable load test and a report, wiring together:

- **Load engine:** Locust (industry-standard, distributed-capable).
- **Discovery & knowledge:** site crawl, API inventory, an Application Knowledge Base, and a KB of YAML rules + SQLite Memory that carry lessons between runs.
- **Planning:** a Test Plan Analyzer (PDF/DOCX → test-plan.json) and a Performance Planner (→ execution-plan.json) for workload, think time, and SLAs.
- **Generation:** a Locust script generator with dynamic correlation, plus an Intelligent Test Data Generator producing a unified `testdata.csv`.
- **Execution & analysis:** a single-user checkout validator, the load run, RCA/self-heal, and an HTML + Excel report.
- **Optional browser track:** Playwright for payment paths that pure HTTP cannot complete.

---

## Strengths (grounded in what was observed)

**The measurement core is legitimate.** Building on Locust means the load-generation and distribution engine is proven; APEA is not reinventing that layer.

**Correlation actually works against a live store.** In real run artifacts, the customer bearer token and Magento quote ID thread correctly through create → add-items → shipping → payment, and orders were placed successfully across many runs (test100–test116). Correlation and parameterisation are the hard part of any perf script, and APEA does them dynamically rather than with hardcoded values.

**The pipeline is end-to-end.** Recording → discovery → generation → run → report → RCA is fully wired. Most in-house efforts never finish this loop.

**The agentic layer removes real toil.** Test-plan analysis, data generation with live product/address discovery, and a KB/Memory that reuses prior lessons meaningfully reduce manual scripting and re-diagnosis.

**Diagnostics are rich.** Per-step checkout timelines, quote-drift detection, a live JMeter-style call tree, and payment replay analysis make failures inspectable instead of opaque.

---

## Gaps & risks (prioritised)

**1. Reproducibility / determinism — the biggest concern.**
Self-healing retries, automatic payment-method switching, Memory-driven choices, and faithful→rest auto-switching all make a run adapt itself at execution time. Excellent for reaching a passing run; problematic for a measuring instrument, because the applied load profile can differ between runs. For pass/fail validation this is fine; for trending p95/throughput across releases it undermines comparability.
*Fix direction:* a "strict / reproducible" mode that pins the method, disables adaptive healing, and fails loudly instead of self-correcting.

**2. Magento-shaped despite a platform-agnostic goal.**
Adapters and a platform registry exist, but the deep logic — REST checkout state machine, region/shipping/payment handling — is built around Magento. Other platforms (Shopify, SFCC, custom) will need real engineering, not just configuration.

**3. Payment coverage under load.**
Hosted card gateways (e.g. CyberSource/ParadoxLabs) tokenise the card in a third-party iframe that HTTP replay cannot complete, so load runs fall back to offline methods (e.g. netterms). Payment is frequently the real bottleneck in checkout, so the component most likely to fail may be the one *least* exercised at scale. The browser track addresses fidelity but does not scale like HTTP.

**4. Maintainability of the generator.**
The Locust script is produced from a large templated string with token substitution. It works, but it is hard to unit-test and brittle to change — which is consistent with the recurring regressions we have chased. This raises the long-term cost of ownership.

**5. Proven at validation, less proven at sustained concurrency.**
Most inspected evidence is single-user checkout *validation*. Real multi-user behaviour surfaced genuine issues (quote contention, the need for many pre-confirmed accounts, transient store flakiness). The high-concurrency and distributed-worker story needs a real load run to trust.

**6. Data fidelity edge cases.**
Observed cases where product price resolved to 0/None and where the generated CSV's payment method/address did not match what the run used. Cosmetic for raw throughput, but they matter when the test must represent a *realistic* transaction (correct totals, inventory decrement, real payment path). These were partially addressed this session (coherence + annotation fixes) but warrant a systematic pass.

---

## Suitability by use case

| Use case | Fit today | Notes |
|---|---|---|
| Magento checkout load validation (McFadyen commerce clients) | **Strong** | Correlation works; end-to-end; tailored. Main need: reproducible mode + real load run. |
| Regression trending of perf across releases | **Moderate** | Blocked by run-to-run non-determinism until a strict mode exists. |
| Non-Magento commerce platforms | **Emerging** | Framework is there; deep logic needs porting per platform. |
| Generic enterprise perf tool (any app/API) | **Limited** | vanilla Locust / k6 / JMeter give a more predictable, maintainable instrument. |
| Full checkout incl. real hosted-gateway payment at scale | **Partial** | HTTP can't drive hosted iframes; browser track doesn't scale equivalently. |

---

## Hardening roadmap

**Phase 1 — Make it a trustworthy ruler (highest value).**
Add a strict/reproducible run mode: pin payment method and data, disable adaptive self-healing (or log-only), fail loudly on deviation, and stamp each report with the exact effective profile (users, duration, method, think time) rather than the *proposed* plan values. This directly converts APEA from "gets a green run" to "produces comparable measurements."

**Phase 2 — Prove it at scale.**
Execute a real distributed load run (multiple Locust workers, pool of pre-confirmed accounts) once the environment is available, and characterise its own overhead and ceiling. Document the accounts/data prerequisites so multi-user runs don't hit quote contention.

**Phase 3 — Reduce generator fragility.**
Refactor the templated-string generator toward composable, unit-testable building blocks; add a small generated-script test suite so regressions are caught before a run. This lowers ongoing maintenance cost.

**Phase 4 — Broaden platform reach deliberately.**
Pick one non-Magento target and port the checkout state machine through the adapter layer, hardening the abstraction as you go — rather than assuming platform-agnosticism that isn't yet exercised.

**Phase 5 — Payment fidelity at scale.**
Decide, per engagement, whether checkout perf must include the real payment path; if so, invest in the browser-track scaling story or a sanctioned gateway sandbox with replayable tokens, so the bottleneck component is actually load-tested.

---

## Basis & caveats

This assessment is from static code and stored run artifacts only; nothing was executed this session (sandbox down). A single real, multi-user load run would materially sharpen the confidence on scale, overhead, and reproducibility. The strengths around correlation and order placement are well-evidenced by past run logs; the scale-related gaps are informed inference and should be confirmed empirically.
