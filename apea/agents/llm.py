"""Optional LLM layer (Anthropic Claude).

This is the *agentic* brain of APEA. It is entirely optional and runs ONLY off
the load-generation hot path — at build time (discovery, script self-repair) and
post-run (root-cause narrative, Ask APEA). If no API key is configured, every
helper degrades gracefully to a no-op and APEA keeps its deterministic behavior.

Design rules:
  * Never called from inside the generated Locust script / a virtual user.
  * Never raises to the caller — failures return None / falls back.
  * No new hard dependency: talks to the Messages API over `requests`.

Configure with the ANTHROPIC_API_KEY environment variable (and optionally
APEA_LLM_MODEL). The request is made from the machine running APEA.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

API_URL = "https://api.anthropic.com/v1/messages"
_DEFAULT_MODEL = os.environ.get("APEA_LLM_MODEL", "claude-3-5-sonnet-latest")
_ANTHROPIC_VERSION = "2023-06-01"
_TIMEOUT = float(os.environ.get("APEA_LLM_TIMEOUT", "60"))

# --------------------------------------------------------------------------- #
# Persistent (cross-process) result cache.
# The CLI starts a NEW process every run, so its in-memory caches never reuse.
# This on-disk cache lets repeated runs of the SAME recording skip the biggest
# Claude calls (recording analysis + parameterization). Keyed by a STABLE hash
# of the input (not Python's per-process-salted hash()) + the model, so it stays
# valid across runs and invalidates if the model changes. Disable with
# APEA_LLM_CACHE=0. Never raises.
# --------------------------------------------------------------------------- #
_CACHE_ENABLED = os.environ.get("APEA_LLM_CACHE", "1") not in ("0", "false", "False", "")


def _cache_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".apea_llm_cache")


def cache_key(*parts) -> str:
    """Stable hash of the inputs (+ model) — safe across processes/runs."""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "ignore"))
        h.update(b"\x00")
    h.update((_DEFAULT_MODEL or "").encode("utf-8", "ignore"))
    return h.hexdigest()[:40]


def cache_get(namespace: str, key: str):
    if not _CACHE_ENABLED:
        return None
    try:
        p = os.path.join(_cache_dir(), "%s-%s.json" % (namespace, key))
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return None


def cache_put(namespace: str, key: str, value) -> None:
    if not _CACHE_ENABLED:
        return
    try:
        d = _cache_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%s-%s.json" % (namespace, key)), "w",
                  encoding="utf-8") as fh:
            json.dump(value, fh)
    except Exception:
        pass


def api_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("APEA_ANTHROPIC_API_KEY") or "").strip()


# Master AI toggle — lets you keep the key configured but STOP all Claude usage
# (to cap API spend) without editing env or code. Defaults on; override the
# initial state with APEA_LLM_ENABLED=0. Flip at runtime via set_enabled().
_ENABLED = os.environ.get("APEA_LLM_ENABLED", "1") not in ("0", "false", "False", "off", "no", "")


def is_enabled() -> bool:
    return _ENABLED


def set_enabled(on: bool) -> None:
    """Turn all AI usage on/off at runtime (UI toggle / CLI --no-ai)."""
    global _ENABLED
    _ENABLED = bool(on)


def available() -> bool:
    """True when AI is ENABLED, an Anthropic key is configured, and requests is
    importable. The master toggle wins — off means no Claude calls at all."""
    if not _ENABLED:
        return False
    if not api_key():
        return False
    try:
        import requests  # noqa: F401
        return True
    except Exception:
        return False


def status() -> dict:
    """Small dict for the UI to show whether the agentic layer is active."""
    return {"enabled": available(),
            "model": _DEFAULT_MODEL,
            "key_present": bool(api_key()),
            "ai_on": _ENABLED}


def chat(prompt: str, system: str = "", max_tokens: int = 1024,
         model: str | None = None, temperature: float = 0.0) -> str | None:
    """Return the assistant text, or None on any failure. Never raises."""
    key = api_key()
    if not key:
        return None
    try:
        import requests
    except Exception:
        return None
    body = {
        "model": model or _DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    headers = {
        "x-api-key": key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        resp = requests.post(API_URL, headers=headers, json=body, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        parts = data.get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return text.strip() or None
    except Exception:
        return None


def json_call(prompt: str, system: str = "", max_tokens: int = 1500,
              model: str | None = None) -> dict | None:
    """Ask for JSON and parse it robustly (handles ```json fences / stray text)."""
    sys_prompt = (system + "\n\n" if system else "") + (
        "Respond with ONLY a single valid JSON object. No prose, no code fences.")
    out = chat(prompt, system=sys_prompt, max_tokens=max_tokens, model=model)
    if not out:
        return None
    return _extract_json(out)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    # strip code fences if present
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # last resort: grab the outermost {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None
