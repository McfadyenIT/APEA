"""Memory layer — turns each run into knowledge the NEXT run reuses.

Flow:  Run -> Store -> Compare -> Learn -> Improve Generator.

Concretely: after a run, `learn_from_flow()` reads the checkout state + failure
reasons, classifies them against the Knowledge Base (no LLM), and persists what
it learned — both as durable RCA memory and as target-scoped "facts". Before the
next run, `facts_for(target_url)` hands the Generator those facts so, e.g.:

    Run 143  fails on regionId (Magento / UK)
      -> learn_fact(target='mcstaging.radwell.eu', 'region_policy', 'omit_region_id')
    Run 144  reads facts_for(...) and omits regionId up front — no LLM call.

Everything degrades gracefully: DB/KB errors never raise to the caller.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import db
from .knowledge import KB


def _host(target_url: str) -> str:
    try:
        h = urlparse(target_url).netloc or (target_url or "").strip()
        return h.split("@")[-1].split(":")[0].lower()
    except Exception:
        return (target_url or "").strip().lower()


def detect_platform(target_url: str = "", flow: Optional[dict] = None) -> str:
    """Best-effort platform id for KB lookups — delegates to the platform
    registry so scoping/RCA follow real multi-platform detection."""
    try:
        from . import platforms
        disc = {"flow": flow} if isinstance(flow, list) else (flow or {})
        p = platforms.detect(disc if isinstance(disc, dict) else {}, url=target_url)
        return p if p != "generic" else "magento"
    except Exception:
        return "magento"


def facts_for(target_url: str, platform: str = "") -> dict:
    """Merged knowledge the Generator/Orchestrator should apply BEFORE a run:
    platform rules (from the KB) overlaid with target-specific learned facts
    (from Memory). Target facts win — they are observed truth for this site."""
    platform = platform or detect_platform(target_url)
    merged = {"platform": platform}
    try:
        merged.update({f"rule.{k}": v for k, v in KB.platform_rules(platform).items()})
    except Exception:
        pass
    try:
        merged.update(db.recall_facts("platform", platform))
        merged.update(db.recall_facts("target", _host(target_url)))
    except Exception:
        pass
    return merged


def learn_from_flow(target_url: str, flow: dict, run_id: str = "",
                    project_id: Optional[int] = None) -> list[dict]:
    """Inspect a finished run's flow (ltm_flow.json contents) and persist what it
    teaches. Returns the list of learnings recorded (for logging/UI)."""
    learned: list[dict] = []
    if not isinstance(flow, dict):
        return learned
    platform = detect_platform(target_url, flow)
    host = _host(target_url)
    cs = flow.get("checkout_state") or {}
    reason = " ".join(str(x) for x in (cs.get("stop_reason", ""),
                                       cs.get("stopped_at", ""))).strip()
    # also fold in explicit timeline failure bodies
    for e in (flow.get("timeline") or []):
        if not e.get("ok") and e.get("body"):
            reason += " " + str(e["body"])

    # Failure learning — only when there IS a failure reason. (A clean success
    # has no reason; we must NOT return early here or the positive-learning block
    # below would be skipped.)
    if reason:
        bug = KB.classify_error(reason)
        if bug:
            try:
                db.record_rca(error_signature=bug["id"], platform=platform,
                              root_cause=bug.get("root_cause", ""),
                              repair_action=bug.get("repair", ""),
                              resolved=bool(flow.get("orders")), run_id=run_id,
                              project_id=project_id)
            except Exception:
                pass
            learned.append({"bug": bug["id"], "repair": bug.get("repair")})

            # Turn specific recognised bugs into durable, target-scoped facts the
            # Generator can act on next time WITHOUT any LLM or re-derivation.
            fact = _bug_to_fact(bug["id"], reason)
            if fact:
                try:
                    db.learn_fact("target", host, fact[0], fact[1],
                                  source_run_id=run_id)
                except Exception:
                    pass
                learned.append({"fact": fact[0], "value": fact[1], "scope": host})

    # Positive learning: if an order was created, remember the winning approach so
    # the next run applies it up front (Improve Generator) — no LLM.
    if flow.get("orders"):
        try:
            db.learn_fact("target", host, "last_success_build",
                          str(flow.get("build", "")), source_run_id=run_id)
            win_pay = flow.get("winning_payment")
            if win_pay:
                db.learn_fact("target", host, "payment_method", str(win_pay),
                              source_run_id=run_id)
                learned.append({"fact": "payment_method", "value": str(win_pay),
                                "scope": host})
        except Exception:
            pass
        learned.append({"fact": "checkout_succeeded", "value": str(flow.get("orders"))})
    return learned


def _bug_to_fact(bug_id: str, reason: str) -> Optional[tuple[str, str]]:
    """Map a recognised bug to a durable generator directive for this target."""
    return {
        "region_id_type": ("region_policy", "omit_region_id_unless_integer"),
        "product_not_found": ("product_lookup", "resolve_sku_by_search"),
        "product_out_of_stock": ("stock_policy", "stop_when_out_of_stock"),
        "payment_method_unavailable": ("payment_policy", "prefer_offline_method"),
        "jwt_token_rejected": ("token_policy", "accept_jwt"),
        "empty_cart_at_shipping": ("cart_policy", "verify_items_before_shipping"),
    }.get(bug_id)


def learn_from_run_dir(target_url: str, run_dir, run_id: str = "",
                       project_id: Optional[int] = None) -> list[dict]:
    """Convenience: read results/ltm_flow.json from a run dir, then learn."""
    try:
        p = Path(run_dir) / "results" / "ltm_flow.json"
        flow = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        flow = {}
    return learn_from_flow(target_url, flow, run_id=run_id, project_id=project_id)


def learn_order_signal(target_url: str, order_confirmation: Optional[dict],
                       run_id: str = "") -> Optional[dict]:
    """Close the crawl->assertion loop: persist the browser-observed order-success
    signal as a target fact, so the NEXT generation asserts order completion the way
    THIS store actually confirms it (facts_for -> _applied_memory -> the generated
    _ORDER_URL_SIGNALS). Only a URL-type fragment is learned — it is stable and
    replayable; volatile order numbers and generic phrases are intentionally not.
    Best-effort; never raises."""
    try:
        oc = order_confirmation or {}
        sig = str(oc.get("signal") or "").strip()
        if oc.get("confirmed") and oc.get("signal_type") == "url" and len(sig) >= 3:
            db.learn_fact("target", _host(target_url), "order_url_signal", sig,
                          source_run_id=run_id)
            return {"fact": "order_url_signal", "value": sig, "scope": _host(target_url)}
    except Exception:
        pass
    return None
