"""Validation layer — verify a generated script WITHOUT modifying it.

Distinct from the Reviewer (which reconciles findings) and Repair (which rewrites):
Validation is a read-only gate that checks four things and returns a verdict the
Orchestrator uses to decide continue vs repair:

    1. syntax        — the file actually compiles
    2. business rules— the checkout state machine and its guards are present
    3. correctness   — no hardcoded runtime values (quote id, card token, string regionId)
    4. performance   — Locust hygiene (catch_response, wait_time, no blocking sleep)

`validate(script)` returns `(ok, issues)` where `issues` is a list of
`{severity, rule, message}` and `ok` is True when there are no CRITICAL issues.
It never modifies the script and never raises.
"""
from __future__ import annotations

import re

CRITICAL, HIGH, LOW = "critical", "high", "low"


def _issue(sev, rule, msg):
    return {"severity": sev, "rule": rule, "message": msg}


def validate(script: str, discovery: dict | None = None,
             plan_cfg: dict | None = None) -> tuple[bool, list[dict]]:
    """Static validation of a generated locustfile. Returns (ok, issues)."""
    issues: list[dict] = []
    src = script or ""

    # 1) syntax -------------------------------------------------------------
    try:
        compile(src, "<generated>", "exec")
    except SyntaxError as exc:
        issues.append(_issue(CRITICAL, "syntax", "does not compile: %s" % exc))
        return False, issues        # nothing else is meaningful if it won't parse

    is_rest = "_rest_checkout" in src or "carts/mine" in src

    # 2) business rules (only meaningful for a REST checkout script) --------
    if is_rest:
        required = {
            "cart create": r"carts/mine",
            "add item": r"carts/mine/items",
            "verify items": r"GET.*carts/mine/items|Cart contains items|item_count",
            "shipping": r"shipping-information",
            "payment methods": r"payment-methods",
            "place order": r"payment-information",
        }
        for label, pat in required.items():
            if not re.search(pat, src):
                issues.append(_issue(HIGH, "business-flow",
                                     "checkout step missing: %s" % label))
        if "_stop(" not in src and "def _stop" not in src:
            issues.append(_issue(CRITICAL, "fail-fast",
                                 "no fail-fast stop — script continues past failures"))

    # 3) correctness — no hardcoded runtime values. These often appear inside the
    # recorded FLOW_STEPS data (replayed only in recorded mode; the REST validator
    # ignores them), so they are HIGH (surfaced) not CRITICAL (blocking).
    if re.search(r'"cartId"\s*:\s*"\d{3,}"', src):
        issues.append(_issue(HIGH, "hardcoded-quote",
                             'hardcoded cartId literal present — must come from POST /carts/mine'))
    if re.search(r'"(?:card_id|payment_token|payerauth_session_id)"\s*:\s*"[^"]{6,}"', src):
        issues.append(_issue(HIGH, "replayed-payment-token",
                             "hardcoded card/payment token present — single-use, cannot be replayed"))
    if re.search(r'"regionId"\s*:\s*"[A-Za-z]', src):
        issues.append(_issue(HIGH, "region-type",
                             'regionId is a string literal — Magento expects an integer'))

    # 4) performance / Locust hygiene ---------------------------------------
    if "catch_response" not in src:
        issues.append(_issue(HIGH, "catch-response",
                             "no catch_response — failed business logic can't surface"))
    if not re.search(r"wait_time\s*=", src):
        issues.append(_issue(LOW, "wait-time", "no wait_time — load shape may be unrealistic"))
    if re.search(r"\btime\.sleep\s*\(", src):
        issues.append(_issue(HIGH, "blocking-sleep",
                             "time.sleep() blocks the greenlet — use Locust wait mechanisms"))

    ok = not any(i["severity"] == CRITICAL for i in issues)
    return ok, issues


def summarize(issues: list[dict]) -> str:
    """One-line signature of the issues (fed to the Orchestrator's error_of)."""
    return " | ".join("[%s] %s: %s" % (i["severity"], i["rule"], i["message"])
                      for i in (issues or []))
