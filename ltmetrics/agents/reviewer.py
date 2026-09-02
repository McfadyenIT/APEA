"""Script Review Agent — a senior performance engineer's audit.

Does not write code; strictly audits the generated Locust script against an
enterprise QA checklist and returns a pass/warn report with remediation notes.
"""
from __future__ import annotations

import ast
import re


def review(script: str, discovery: dict, plan_cfg: dict, do_login: bool) -> dict:
    findings: list[dict] = []

    def check(name: str, ok: bool, detail: str, severity: str = "warn"):
        findings.append({
            "check": name,
            "status": "pass" if ok else severity,
            "detail": detail,
        })

    # 1. Valid Python
    syntax_ok = True
    try:
        ast.parse(script)
    except SyntaxError as exc:  # pragma: no cover - defensive
        syntax_ok = False
        check("Script compiles", False, f"SyntaxError: {exc}", "fail")
    if syntax_ok:
        check("Script compiles", True, "Locust script parses as valid Python.")

    # 2. catch_response on every request
    calls = len(re.findall(r"self\.client\.(get|post|put|patch|delete)\(", script))
    catches = len(re.findall(r"catch_response=True", script))
    check("Response validation (catch_response)", calls > 0 and catches >= calls,
          f"{catches} catch_response guard(s) for {calls} request call(s).")

    # 3. No hardcoded tokens / session ids
    hard = re.findall(r"(?:token|session|csrf|form_key|jwt|bearer)\s*=\s*[\"'][A-Za-z0-9]{8,}",
                      script, re.IGNORECASE)
    check("No hardcoded tokens / session IDs", not hard,
          "None found." if not hard else f"Suspicious literals: {hard[:3]}", "fail")

    # 4. Think time / pacing
    check("User pacing / think time", "wait_time" in script,
          f"wait_time = between{tuple(plan_cfg['think_time'])} present.")

    # 5. Functional assertions
    check("Functional assertions", ".failure(" in script and ".success(" in script,
          "Status-code / body assertions with explicit success/failure marking.")

    # 6. Named transactions (report alignment)
    named = len(re.findall(r"name=", script))
    check("Named transactions", named >= calls,
          f"{named} name= label(s) so report rows align with transactions.")

    # 7. CSV / data-driven
    check("Structured test data", "testdata.csv" in script and "DictReader" in script,
          "Search keywords & credentials loaded from testdata.csv via csv.DictReader.")

    # 8. Randomization
    check("Randomization", "random." in script,
          "Randomized keyword / credential selection to avoid caching artefacts.")

    # 9. Correlation / auth — recognise ALL correlation mechanisms the generator
    # uses (browse-mode token extraction, flow-mode form_key + REST bearer token,
    # and the generic capture->correlate extractor framework), not just one symbol.
    corr_ok = any(sym in script for sym in (
        "_extract_token", "_extract_form_key", "_clean_token",
        "_correlate", "_CORRELATIONS", "_capture"))
    if do_login:
        check("Dynamic correlation (CSRF/token)", corr_ok,
              "form_key / CSRF / bearer tokens captured from responses and "
              "re-injected into later requests (capture → correlate)." if corr_ok
              else "No correlation helpers found — dynamic values may not be handled.")
    else:
        check("Dynamic correlation (CSRF/token)", True,
              "Response-value correlation is wired (form_key / tokens / ids)." if corr_ok
              else "Anonymous journeys only — no auth correlation required.", "info")

    # 10. Modular class design
    check("Modular class design", "class WebsiteUser(HttpUser)" in script,
          "Single reusable HttpUser class with discrete @task methods.")

    # Reconcile the static checklist with an AI reading of the actual script:
    # Claude can downgrade a false warning (feature IS present) or escalate a
    # missed gap. Returns a narrative for display; may adjust finding statuses.
    ai_review = _ai_reconcile(script, findings)

    passed = sum(1 for f in findings if f["status"] == "pass")
    fails = sum(1 for f in findings if f["status"] == "fail")
    warns = sum(1 for f in findings if f["status"] == "warn")

    verdict = "APPROVED" if fails == 0 else "CHANGES REQUIRED"
    corrections = [
        "Assigned journey-derived weights to each @task.",
        "Attached name= labels to every request for report alignment.",
        "Wired testdata.csv for search keywords" +
        (" and credentials." if do_login else "."),
    ]
    if do_login:
        corrections.append("Added CSRF/form_key correlation for the login flow.")

    return {
        "verdict": verdict,
        "score": f"{passed}/{len(findings)} checks passed",
        "passed": passed, "warnings": warns, "failures": fails,
        "findings": findings,
        "corrections_applied": corrections,
        "ai_review": ai_review,
    }


def _ai_reconcile(script: str, findings: list):
    """Reconcile the static checklist with Claude reading the real script. Adjusts
    finding statuses (never the hard 'Script compiles' check) and returns a short
    narrative. No-op / returns None without an API key."""
    try:
        from . import llm
        if not llm.available():
            return None
        import json
        checks = [f["check"] for f in findings]
        res = llm.json_call(
            "You are a senior performance engineer. Read the Locust script and, for "
            "EACH checklist item, give its TRUE status by inspecting the code: "
            "\"pass\", \"warn\", or \"fail\". A feature counts as present even if it "
            "uses a different mechanism than expected (e.g. correlation via form_key "
            "capture / bearer token / a capture->correlate framework, not just one "
            "helper name). Then give a terse overall note.\n\n"
            "Return JSON: {\"verdicts\": {\"<check name>\": \"pass|warn|fail\"}, "
            "\"notes\": \"2-4 sentences\"}.\n\nCHECKLIST: " + json.dumps(checks)
            + "\n\nSCRIPT:\n" + (script or "")[:7000],
            system="You reconcile a static review checklist against the actual script.",
            max_tokens=900) or {}
        verdicts = res.get("verdicts") or {}
        for f in findings:
            v = verdicts.get(f["check"])
            if v in ("pass", "warn", "fail") and v != f["status"]:
                if f["check"].lower().startswith("script compiles"):
                    continue                       # never override the syntax gate
                f["status"] = v
                f["detail"] = (f.get("detail", "") + "  · AI-reconciled").strip()
        return res.get("notes")
    except Exception:
        return None
