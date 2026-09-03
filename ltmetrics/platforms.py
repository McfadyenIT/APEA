"""Platform plugin registry — makes LT Metrics platform-agnostic.

Detection is data-driven: each platform declares `detect_any_in_path` signatures
in `knowledge/rules/platform_rules.yaml`. `detect()` scores the run's evidence
(URL, tech, recorded request paths) against every platform block and returns the
best match; `checkout_mode()` reports whether that platform has a full REST
checkout adapter or should fall back to recorded replay.

Adding a platform = adding a KB block (and, later, a checkout adapter). No code
here changes. Magento is the only platform with a full REST checkout today.
"""
from __future__ import annotations

import json
from typing import Optional

from .knowledge import KB

# Prefer more specific platforms before the generic REST/GraphQL catch-alls.
_PRIORITY = ["magento", "shopify", "salesforce_commerce", "sap_commerce",
             "bigcommerce", "oracle_commerce", "graphql", "custom_rest"]


def _haystack(discovery: Optional[dict], url: str, flow) -> str:
    d = discovery or {}
    parts = [url or "", str(d.get("base_url", "")), str(d.get("domain", "")),
             " ".join(map(str, d.get("tech", []) or []))]
    steps = d.get("flow") or flow or []
    for s in (steps or [])[:200]:
        if isinstance(s, dict):
            parts.append(str(s.get("path", "")))
            parts.append(str((s.get("hdrs") or {})))
    return " ".join(parts).lower()


def detect(discovery: Optional[dict] = None, url: str = "", flow=None) -> str:
    """Return the best-matching platform id, or 'generic' if nothing matches."""
    hay = _haystack(discovery, url, flow)
    order = [p for p in _PRIORITY if p in KB.platforms()] or KB.platforms()
    best, best_score = "generic", 0
    for p in order:
        sigs = KB.platform_rules(p).get("detect_any_in_path", []) or []
        score = sum(1 for sig in sigs if str(sig).lower() in hay)
        if score > best_score:
            best, best_score = p, score
    return best


def checkout_mode(platform: str) -> str:
    """'rest-checkout' if the platform has a full adapter, else 'recorded-replay'."""
    mode = str(KB.platform_rules(platform).get("checkout", "")).lower()
    return "rest-checkout" if mode == "rest" else "recorded-replay"


def info(platform: str) -> dict:
    r = KB.platform_rules(platform)
    return {"platform": platform, "display_name": r.get("display_name", platform),
            "checkout_mode": checkout_mode(platform)}
