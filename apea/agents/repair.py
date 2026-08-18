"""Pre-flight AI self-repair agent (optional, keyed).

The core agentic loop: observe -> reason -> fix -> verify, kept entirely OFF the
load-generation hot path. Before the real load test, run a tiny 1-user pre-flight
against the generated script, read the failures, and let Claude reason out a
bounded set of correlation fixes. Apply them as `discovery["repair_hints"]`,
regenerate the script deterministically, then hand the repaired script to the
real run.

Only SAFE, bounded knobs are ever applied (headers, login URLs, REST prefix,
payment method). The LLM never writes executable code into the script.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import llm
from .. import db
from ..knowledge import KB

PREFLIGHT_SECONDS = int(os.environ.get("APEA_PREFLIGHT_SECONDS", "18"))
# Business-value pre-flight gate. ON by default: it is the only check that catches
# a run which creates orders worth nothing. Disable with APEA_PREFLIGHT_GATE=0.
PREFLIGHT_GATE = (os.environ.get("APEA_PREFLIGHT_GATE", "1").strip().lower()
                  not in ("0", "false", "no", "off"))


def _kb_rules_snippet() -> str:
    """Authoritative platform rules injected into the LLM prompt so Claude
    consumes the shared Knowledge Base instead of guessing."""
    try:
        rules = KB.platform_rules("magento")
        keys = ("rest_prefix_template", "token_regex", "region_id_is_integer",
                "omit_region_when_not_integer", "offline_payments",
                "never_replay_payment_token", "csv_value_is_search_term",
                "stop_when_out_of_stock")
        return json.dumps({k: rules.get(k) for k in keys if k in rules}, indent=2)
    except Exception:
        return "{}"


def _kb_diagnose(failures: list[dict], platform: str = "magento",
                 run_id: str = "") -> dict:
    """Deterministic KB-first pass over the failures: classify each against the
    known-bug catalogue and record it in RCA memory. Returns a summary so the
    caller can skip the LLM when the failure is already understood."""
    err = " ".join((f.get("error") or "") for f in (failures or []))
    bug = KB.classify_error(err)
    repair = KB.repair_for(err)
    out = {"matched": bool(bug), "bug": (bug or {}).get("id", ""),
           "root_cause": (bug or {}).get("root_cause", ""),
           "repair_action": (repair or {}).get("action", "")}
    if bug:
        try:
            db.record_rca(error_signature=bug["id"], platform=platform,
                          root_cause=bug.get("root_cause", ""),
                          repair_action=bug.get("repair", ""), run_id=run_id)
        except Exception:
            pass
    return out

_SYSTEM = (
    "You are a senior performance engineer debugging why an automated checkout "
    "load-test script fails to complete an order on an e-commerce site (Magento, "
    "Shopify, WooCommerce, SFCC, custom B2B/B2C). You are given the discovered "
    "site context and the actual failing requests from a 1-user dry run. Propose "
    "the minimal, safe configuration fixes that would let the checkout/quote "
    "complete. Do not invent endpoints; base fixes on the evidence."
)


def available() -> bool:
    return llm.available()


def _launcher() -> list[str]:
    b = shutil.which("locust")
    return [b] if b else [sys.executable, "-m", "locust"]


def _run_preflight(run_dir: Path, base_url: str) -> None:
    cmd = [*_launcher(), "-f", "scripts/locustfile.py", "--host", base_url,
           "--headless", "-u", "1", "-r", "1",
           "--run-time", f"{PREFLIGHT_SECONDS}s",
           "--csv", "results/preflight", "--only-summary", "--loglevel", "WARNING"]
    log = run_dir / "results" / "preflight_run.log"
    try:
        with open(log, "w", encoding="utf-8") as fh:
            subprocess.run(cmd, cwd=str(run_dir), stdout=fh, stderr=subprocess.STDOUT,
                           timeout=PREFLIGHT_SECONDS + 40)
    except Exception:
        pass


def _read_failures(run_dir: Path, limit: int = 25,
                   name: str = "preflight_failures.csv") -> list[dict]:
    p = run_dir / "results" / name
    if not p.exists():
        return []
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        return []
    out = []
    for r in rows[:limit]:
        out.append({"method": r.get("Method", ""), "name": r.get("Name", ""),
                    "error": (r.get("Error", "") or "")[:400],
                    "count": r.get("Occurrences", "")})
    return out


def _read_flow(run_dir: Path) -> dict:
    p = run_dir / "results" / "apea_flow.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def preflight_value_gate(run_dir, base_url: str) -> dict:
    """Run ONE user through the journey and check the transaction is REAL before
    spending a whole load run on it.

    A load test that returns 200 everywhere but creates orders worth nothing looks
    like a pass in every other report — that is the failure this gate exists for.

    Verdicts:
      pass     an order was created AND carried a non-zero value
      blocked  an order was created but is worth 0 — the load run would write
               worthless orders to the store and still report success
      warn     no order completed; could be transient (stock, a flaky login), so
               the caller continues — the full run has many more attempts
      skipped  gate disabled, or the pre-flight could not be evaluated

    Costs one real order against the target. Never raises.
    """
    run_dir = Path(run_dir)
    if not PREFLIGHT_GATE:
        return {"verdict": "skipped", "ok": True,
                "reason": "pre-flight value gate disabled (APEA_PREFLIGHT_GATE=0)"}
    try:
        _run_preflight(run_dir, base_url)
        flow = _read_flow(run_dir)
        orders = int(flow.get("orders") or 0)
        values = [v for v in (flow.get("order_values") or [])
                  if isinstance(v, (int, float))]
        total = float(flow.get("order_value_total") or 0.0)
        state = flow.get("checkout_state") or {}
        if orders <= 0:
            return {"verdict": "warn", "ok": True, "orders": 0, "value_total": 0.0,
                    "reason": ("pre-flight placed no order (%s) — continuing, because "
                               "this is often transient (stock, a flaky login) and the "
                               "full run retries many times"
                               % (state.get("stop_reason") or "no reason recorded"))}
        if (values and not any(v > 0 for v in values)) or (not values and total <= 0):
            return {"verdict": "blocked", "ok": False, "orders": orders,
                    "value_total": total,
                    "reason": ("pre-flight created %d order(s) with a total value of %s. "
                               "The product price is not reaching the order, so a load run "
                               "would write worthless orders to the store and still report "
                               "a pass. Fix the test data or the pricing path and re-run, "
                               "or set APEA_PREFLIGHT_GATE=0 to override."
                               % (orders, total))}
        return {"verdict": "pass", "ok": True, "orders": orders, "value_total": total,
                "reason": "pre-flight placed %d order(s) worth %s" % (orders, total)}
    except Exception as exc:
        return {"verdict": "skipped", "ok": True,
                "reason": "pre-flight value gate error: %s: %s"
                          % (type(exc).__name__, exc)}


def _site_context(discovery: dict) -> dict:
    flow = discovery.get("flow") or []
    steps = [{"method": s.get("method"), "path": s.get("path"),
              "rest": s.get("rest"), "login": bool(s.get("login"))}
             for s in flow[:40]]
    return {
        "base_url": discovery.get("base_url"),
        "tech": discovery.get("tech"),
        "login_form_action": (discovery.get("login_form") or {}).get("action"),
        "flow_steps": steps,
    }


def _sanitize(hints: dict) -> dict:
    """Keep only the safe, expected keys with safe types."""
    if not isinstance(hints, dict):
        return {}
    out: dict = {}
    lu = hints.get("login_urls")
    if isinstance(lu, list):
        out["login_urls"] = [str(x) for x in lu if isinstance(x, str) and x.startswith("/")][:5]
    if isinstance(hints.get("rest_prefix"), str) and hints["rest_prefix"].startswith("/rest"):
        out["rest_prefix"] = hints["rest_prefix"]
    eh = hints.get("extra_headers")
    if isinstance(eh, dict):
        out["extra_headers"] = {str(k): str(v) for k, v in list(eh.items())[:10]}
    if isinstance(hints.get("payment_method"), str):
        out["payment_method"] = hints["payment_method"][:40]
    if isinstance(hints.get("diagnosis"), str):
        out["diagnosis"] = hints["diagnosis"][:1200]
    return out


def preflight_repair(run_dir: Path, discovery: dict, regenerate) -> dict:
    """Run a dry-run, ask Claude for fixes, apply, regenerate. Never raises.

    `regenerate(discovery)` re-emits the locustfile with the merged hints.
    Returns a small dict describing what happened (also written to repair.json).
    """
    result = {"ran": False, "repaired": False, "reason": "", "diagnosis": "",
              "hints": {}}
    try:
        if not llm.available():
            result["reason"] = "no API key"
            return result
        base = discovery.get("base_url")
        if not base or not (discovery.get("flow")):
            result["reason"] = "no recorded checkout flow to repair"
            return result

        result["ran"] = True
        _run_preflight(Path(run_dir), base)
        flow = _read_flow(Path(run_dir))
        failures = _read_failures(Path(run_dir))

        # Already healthy? then don't touch anything.
        if flow.get("orders", 0) and len(failures) <= 1:
            result["reason"] = "pre-flight already created an order — no repair needed"
            return result
        if not failures:
            result["reason"] = "no failing requests captured in pre-flight"
            return result

        prompt = (
            "SITE CONTEXT:\n" + json.dumps(_site_context(discovery), indent=2)
            + "\n\nPRE-FLIGHT COUNTERS:\n" + json.dumps(flow)
            + "\n\nFAILING REQUESTS (method, name, error body):\n"
            + json.dumps(failures, indent=2)
            + "\n\nReturn a JSON object with these OPTIONAL keys only:\n"
            "  diagnosis: string (why the order/quote failed)\n"
            "  login_urls: array of absolute paths to try for login (e.g. \"/customer/ajax/login\")\n"
            "  rest_prefix: string like \"/rest/<store>/V1\" if the REST store prefix is wrong\n"
            "  extra_headers: object of headers to add to every checkout request\n"
            "  payment_method: string offline payment code to force (e.g. \"checkmo\", \"purchaseorder\")\n"
            "Base every field on the evidence. Omit a key if unsure."
        )
        hints = llm.json_call(prompt, system=_SYSTEM, max_tokens=1200) or {}
        hints = _sanitize(hints)
        result["diagnosis"] = hints.get("diagnosis", "")
        result["hints"] = hints

        applied = {k: v for k, v in hints.items() if k != "diagnosis"}
        if not applied:
            result["reason"] = "AI found no safe automatic fix"
            return result

        discovery["repair_hints"] = applied
        regenerate(discovery)     # rewrite locustfile with the merged hints
        result["repaired"] = True
        result["reason"] = "applied AI fixes and regenerated the script"
        return result
    except Exception as exc:
        result["reason"] = f"repair skipped: {type(exc).__name__}: {exc}"
        return result
    finally:
        try:
            (Path(run_dir) / "repair.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8")
        except Exception:
            pass


def post_run_repair(run_dir: Path, discovery: dict, regenerate) -> dict:
    """Closed-loop auto-heal driven by the ACTUAL run failures (no dry run).

    Reads the just-finished run's failures + counters, asks Claude for concrete
    fixes, merges them into discovery['repair_hints'], and regenerates the script
    so the caller can re-run. Never raises. Returns {repaired, reason, diagnosis}.
    """
    result = {"repaired": False, "reason": "", "diagnosis": "", "hints": {}}
    try:
        if not llm.available():
            result["reason"] = "no API key"
            return result
        failures = _read_failures(Path(run_dir), name="locust_failures.csv")
        flow = _read_flow(Path(run_dir))
        if not failures:
            result["reason"] = "no failing requests to heal"
            return result

        # KB-FIRST: if this failure is already in the known-bug catalogue, the
        # deterministic fix is already encoded in the generator — record RCA
        # memory and skip the LLM entirely (accumulated knowledge cuts LLM usage).
        kb = _kb_diagnose(failures, run_id=str(Path(run_dir).name))
        result["kb"] = kb
        if kb.get("matched"):
            result["diagnosis"] = "Known issue [%s]: %s" % (kb["bug"], kb["root_cause"])
            result["reason"] = ("recognised by knowledge base (no LLM) — deterministic "
                                "fix '%s' is applied by the generator; re-run to confirm"
                                % (kb.get("repair_action") or "n/a"))
            return result

        prev = (discovery.get("repair_hints") or {})
        prompt = (
            "A load-test run FAILED. Here is the evidence. Propose concrete, safe "
            "fixes so the checkout/journey completes on the next run.\n\n"
            "AUTHORITATIVE PLATFORM RULES (follow these; do not contradict them):\n"
            + _kb_rules_snippet()
            + "\n\nSITE CONTEXT:\n" + json.dumps(_site_context(discovery), indent=2)
            + "\n\nRUN COUNTERS:\n" + json.dumps(flow)
            + "\n\nFIXES ALREADY APPLIED (avoid repeating):\n" + json.dumps(prev)
            + "\n\nFAILING REQUESTS (method, name, full error body):\n"
            + json.dumps(failures, indent=2)
            + "\n\nReturn a JSON object with these OPTIONAL keys only:\n"
            "  diagnosis: string (the root cause)\n"
            "  login_urls: array of absolute login paths to try\n"
            "  rest_prefix: string like \"/rest/<store>/V1\" if the REST prefix is wrong\n"
            "  extra_headers: object of headers to add to every request\n"
            "  payment_method: offline payment code to force (e.g. \"checkmo\", \"purchaseorder\")\n"
            "Base every field strictly on the evidence; omit anything you're unsure of."
        )
        hints = _sanitize(llm.json_call(prompt, system=_SYSTEM, max_tokens=1200) or {})
        result["diagnosis"] = hints.get("diagnosis", "")
        applied = {k: v for k, v in hints.items() if k != "diagnosis"}
        # merge over any prior hints (don't lose earlier fixes)
        merged = dict(prev)
        merged.update(applied)
        result["hints"] = merged
        if not applied:
            result["reason"] = result["diagnosis"] or "AI found no further safe fix"
            return result
        discovery["repair_hints"] = merged
        regenerate(discovery)
        result["repaired"] = True
        result["reason"] = "applied AI fixes and regenerated the script"
        return result
    except Exception as exc:
        result["reason"] = f"heal skipped: {type(exc).__name__}: {exc}"
        return result
    finally:
        try:
            (Path(run_dir) / "heal.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8")
        except Exception:
            pass
