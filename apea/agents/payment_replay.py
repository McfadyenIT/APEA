"""Payment replay-profile classifier — can the card step run at the HTTP layer?

APEA's browser Track B can now CAPTURE the real payment gateway/iframe network
sequence (browser_track.payment_network, recorded by browser_runner_gen with PANs
and secrets redacted). This module turns that captured evidence — combined with
KB knowledge in browser_patterns.yaml:replay_profiles — into an evidence-based
verdict:

    replayable = true         a Locust VU can create real sandbox orders over HTTP
                              by re-minting a fresh token/signed-params per VU
                 conditional  possible only if specific conditions hold (the KB
                              lists them; the capture says whether they're met)
                 false        a hard blocker makes HTTP replay impossible
                 unknown      not enough evidence / no profile — needs a look

This replaces the old blanket assumption ("hosted gateways can never be replayed")
with a per-recording determination. Nothing here is vendor-specific in the code —
the vendor knowledge lives in the KB; the code reasons over VALUES and the captured
sequence, so a new gateway is taught by adding a KB block, not by editing this file.

Pure analysis: it never drives a browser, never places an order, never persists a
secret. It only reads the already-redacted capture. Never raises; returns None when
there's nothing to say.
"""
from __future__ import annotations

import re
from pathlib import Path

# Tokens in a captured request body that mark a SINGLE-USE, server-signed request
# (CyberSource Secure Acceptance & friends) — a captured copy can't be replayed.
_SINGLE_USE_HINT = re.compile(
    r"(signature|transaction_uuid|signed_date_time|access_key|signed_field_names|"
    r"capture_context|flexresponse)", re.I)
# Requests that look like CLIENT-side tokenization (re-mintable per VU).
_TOKENIZE_HINT = re.compile(
    r"(payment_methods|/tokens|createToken|microform|tokeniz|elements)", re.I)
# Requests that look like the merchant's own "get signed params" endpoint.
_GETPARAMS_HINT = re.compile(r"(getparams|secureaccept|/sign|signature)", re.I)


def _kb() -> dict:
    """Load replay_profiles + signatures from the KB (best-effort)."""
    try:
        import yaml
        p = Path(__file__).resolve().parent.parent / "knowledge" / "rules" / "browser_patterns.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return data.get("replay_profiles") or {}
    except Exception:
        return {}


def _infer_gateway(captured: list, signatures: dict) -> str:
    """Map captured hosts/paths to a KB profile key when the runner didn't tag one."""
    blob = " ".join((c.get("url") or "") for c in captured).lower()
    for gw, sigs in (signatures or {}).items():
        for s in (sigs or []):
            if str(s).lower() in blob:
                return gw
    return ""


def _pick(captured: list, rx: re.Pattern) -> str | None:
    for c in captured:
        u = c.get("url") or ""
        body = c.get("post_data") or ""
        if rx.search(u) or rx.search(str(body)):
            return u[:200]
    return None


def _evidence(captured: list) -> dict:
    """What the captured sequence actually shows."""
    single_use = any(_SINGLE_USE_HINT.search(str(c.get("post_data") or "")) or
                     _SINGLE_USE_HINT.search(c.get("url") or "") for c in captured)
    return {
        "captured_count": len(captured),
        "has_single_use_token": single_use,
        "tokenize_endpoint": _pick(captured, _TOKENIZE_HINT),
        "getparams_endpoint": _pick(captured, _GETPARAMS_HINT),
    }


def classify(browser_track: dict | None, detected_gateway: str = "") -> dict | None:
    """Return the replay-profile verdict, or None if there's nothing to assess."""
    try:
        bt = browser_track or {}
        captured = bt.get("payment_network") or []
        gateway = (detected_gateway or bt.get("gateway") or "").strip()

        profiles = _kb()
        signatures = profiles.get("_signatures") or {}
        if not gateway:
            gateway = _infer_gateway(captured, signatures)

        # Nothing observed and no gateway known -> nothing useful to say.
        if not gateway and not captured:
            return None

        prof = profiles.get(gateway) or profiles.get("_default") or {
            "replayable": "unknown", "mechanism": "undetermined", "reasons": []}
        ev = _evidence(captured)

        replayable = prof.get("replayable", "unknown")
        reasons = list(prof.get("reasons") or [])
        blockers = list(prof.get("blockers") or [])
        confidence = "medium"

        # --- reconcile KB stance with captured evidence -----------------------
        if ev["has_single_use_token"]:
            blockers.append(
                "Captured request carries single-use/server-signed fields "
                "(e.g. signature/transaction_uuid) — a recorded copy cannot be "
                "replayed as-is; each VU would need freshly signed params.")
            if replayable == "unknown":
                replayable = "conditional"
        if ev["tokenize_endpoint"]:
            reasons.append("Captured a client-tokenization request (%s) — a fresh "
                           "token is mintable per VU." % ev["tokenize_endpoint"])
            if replayable == "unknown":
                replayable = "conditional"
        if ev["getparams_endpoint"]:
            reasons.append("Captured a merchant sign/getParams endpoint (%s) that "
                           "can be called per VU to mint fresh params." % ev["getparams_endpoint"])

        # confidence scales with how much we actually observed
        if ev["captured_count"] >= 3 and (ev["tokenize_endpoint"] or ev["getparams_endpoint"]):
            confidence = "high"
        elif ev["captured_count"] == 0:
            confidence = "low"

        # resolve the mint/confirm recipe: prefer captured endpoints over KB hints
        mint = dict(prof.get("mint") or {})
        if ev["tokenize_endpoint"]:
            mint["resolved_mint_endpoint"] = ev["tokenize_endpoint"]
        elif ev["getparams_endpoint"]:
            mint["resolved_mint_endpoint"] = ev["getparams_endpoint"]

        return {
            "gateway": gateway or "unknown",
            "replayable": replayable,
            "mechanism": prof.get("mechanism", "undetermined"),
            "confidence": confidence,
            "reasons": reasons,
            "blockers": blockers,
            "mint": mint or None,
            "confirm_endpoint_hint": prof.get("confirm_endpoint_hint"),
            "evidence": ev,
            "captured": captured[:20],
            # a plain-English one-liner for the report header
            "verdict": _verdict_line(gateway or "unknown", replayable, ev),
        }
    except Exception:
        return None


def _verdict_line(gateway: str, replayable, ev: dict) -> str:
    n = ev.get("captured_count", 0)
    seen = ("%d payment request(s) captured" % n) if n else "no payment traffic captured"
    if replayable is True or replayable == "true":
        return ("%s: HTTP-replayable — a VU can mint a fresh sandbox token per "
                "iteration and create real orders without a browser (%s)." % (gateway, seen))
    if replayable == "conditional":
        return ("%s: conditionally replayable — depends on re-minting signed params/"
                "tokens per VU; verify the conditions below against the capture (%s)." % (gateway, seen))
    if replayable is False or replayable == "false":
        return ("%s: NOT HTTP-replayable — a hard blocker prevents replay; use the "
                "browser track for real card orders and offline payment for bulk load (%s)." % (gateway, seen))
    return "%s: replayability undetermined — %s." % (gateway, seen)
