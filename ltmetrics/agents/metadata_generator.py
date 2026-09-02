"""Recording metadata extraction — the pieces that STRENGTHEN the deterministic
HTTP flow but never drive it: checkout-agreement (T&C) ids, Selenium-typed
credential/search inputs, the full ordered browser-action list, and the optional
LLM analysis of the whole recording.

Split out of recording.py (behavior-preserving, verbatim moves). Depends only on
the sibling `llm` module (loaded lazily, degrades to {} / [] with no API key).
"""
from __future__ import annotations

import re


def _extract_agreement_ids(flow) -> list:
    """Pull agreement_ids from any recorded request body (place-order/payment)."""
    import json
    ids: list = []
    for s in (flow or []):
        b = s.get("body")
        txt = b if isinstance(b, str) else (json.dumps(b) if isinstance(b, (dict, list)) else "")
        for m in re.finditer(r'"agreement_ids"\s*:\s*\[([^\]]*)\]', txt or ""):
            for tok in re.findall(r'"?([A-Za-z0-9_\-]+)"?', m.group(1)):
                if tok and tok not in ids:
                    ids.append(tok)
    return ids


# Matches BlazeMeter Selenium type actions, e.g.
#   - typeByID(form-login-username): "dartmouth@yopmail.com"
#   - typeSecretByID(form-login-password): "Test@123"
#   - typeByID(search): "70121-1389-07"
_SEL_TYPE_RE = re.compile(
    r'type(Secret)?By(?:ID|Name|CSS|XPath|LinkText)\(([^)]*)\)\s*:\s*(.+?)\s*$', re.I)

_AI_ANALYSIS_CACHE: dict = {}   # hash(recording text) -> analysis (one LLM call per file)


# --------------------------------------------------------------------------- #
# structured Selenium/browser-action steps (kept for display + parameterization,
# never used to drive the replayed HTTP `flow` above)
# --------------------------------------------------------------------------- #
# `_walk_do` deliberately skips any Taurus request whose only content is an
# `actions:` block ("requests with only 'actions' (Selenium) are ignored") —
# only the HTTP `do:` steps are replayed. That's correct for script generation,
# but it means every recorded UI action (click, type, wait, select...) simply
# vanishes from view; only `_extract_selenium_inputs` below claws back three
# hardcoded field categories. `_extract_selenium_steps` recovers ALL of them,
# in recorded order, as structured entries — used for the analysis report and
# for fuller field detection, never for HTTP replay (so it can't destabilize
# the generator or executor).
#
# This reads the RAW TEXT, not the parsed YAML `data` dict, on purpose: when
# `_load_yaml_resilient` has to fall back to its Tier-1 repair, it neutralizes
# each malformed action line to a bare placeholder ("- selenium_action") so
# the file parses at all — by the time `data` exists, the real action content
# is already gone. The raw text always has it, repaired or not.
_ACTIONS_KEY_RE = re.compile(r"^(?P<indent>[ \t]*)actions:\s*(#.*)?$")
_LABEL_KEY_RE = re.compile(r"^[ \t]*-?\s*label:\s*(.+?)\s*$")
_YAML_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)-\s*(?P<content>.*?)\s*$")
_ACTION_CALL_RE = re.compile(
    r'^[\'"]?(?P<verb>[A-Za-z][A-Za-z0-9_]*)\((?P<target>[^)]*)\)[\'"]?'
    r'\s*(?::\s*[\'"]?(?P<value>.*?)[\'"]?\s*)?$')


def _parse_action_line(content: str):
    """One `actions:` list-item's content -> {verb, target, value, step_label},
    or None if it doesn't look like a `verb(target): value` / `verb(target)`
    call at all (keeps this tolerant of odd/unknown action syntax)."""
    m = _ACTION_CALL_RE.match(content.strip())
    if not m:
        return None
    verb = m.group("verb")
    target = (m.group("target") or "").strip().strip("'\"")
    value = m.group("value")
    if value is not None:
        value = value.strip().strip("'\"")
    label = f"{verb}({target})" if target else f"{verb}()"
    return {"verb": verb, "target": target, "value": value, "step_label": label}


def _extract_selenium_steps(text: str) -> list[dict]:
    """Every Selenium/Taurus browser action, in recorded order, as a structured
    step: {group, verb, target, value, step_label}. `group` is the nearest
    preceding Taurus request `label:` (the block this action belongs to), or
    "UI" if none was recorded. Best-effort line scan; never raises, and always
    returns [] rather than partial garbage on anything unexpected."""
    steps: list[dict] = []
    try:
        current_label = None
        in_block = False
        actions_indent = 0
        for raw in (text or "").splitlines():
            if not raw.strip():
                continue
            lm = _LABEL_KEY_RE.match(raw)
            if lm:
                current_label = lm.group(1).strip().strip("'\"")[:60]
                in_block = False
                continue
            am = _ACTIONS_KEY_RE.match(raw)
            if am:
                in_block = True
                actions_indent = len(am.group("indent"))
                continue
            if not in_block:
                continue
            indent = len(raw) - len(raw.lstrip(" \t"))
            im = _YAML_LIST_ITEM_RE.match(raw)
            if im is None or indent < actions_indent:
                in_block = False
                continue
            step = _parse_action_line(im.group("content"))
            if step:
                step["group"] = current_label or "UI"
                steps.append(step)
    except Exception:
        return []
    return steps[:500]


def _extract_selenium_inputs(text: str) -> list[dict]:
    """Pull typed credential / search values out of Selenium actions and classify
    them into fields (username / password / search_keyword) so they surface as
    parameterizable CSV columns. First value per field wins. Never raises."""
    found: dict[str, dict] = {}
    try:
        for raw in (text or "").splitlines():
            line = raw.strip().lstrip("-").strip().strip("'\"")
            m = _SEL_TYPE_RE.search(line)
            if not m:
                continue
            secret = bool(m.group(1))
            target = (m.group(2) or "").strip().strip("'\"").lower()
            value = (m.group(3) or "").strip().strip("'\"")
            if secret or "pass" in target:
                field = "password"
            elif "user" in target or "email" in target or "login" in target:
                field = "username"
            elif "search" in target or target in ("q", "search_query"):
                field = "search_keyword"
            else:
                continue        # ignore other typed fields for the sample
            found.setdefault(field, {"field": field, "target": target,
                                     "secret": secret, "sample": value})
    except Exception:
        pass
    return list(found.values())


def _ai_recording_analysis(text: str, flow: list) -> dict:
    """LLM analysis layer for the recording. Reads the Selenium actions + the HTTP
    request list and returns a structured, sanitised understanding used to
    STRENGTHEN (not replace) the deterministic parse:

        {platform, journey[], inputs[{field,value,secret}], warnings[], notes}

    Grounded in the Knowledge Base's field vocabulary. Returns {} with no API key
    or on any failure — so the pipeline never depends on the LLM being present."""
    try:
        from . import llm
        if not llm.available() or not (text or "").strip():
            return {}
        _key = hash(text)                    # parse_recording runs several times/run
        if _key in _AI_ANALYSIS_CACHE:       # — analyze the recording with the LLM once
            return _AI_ANALYSIS_CACHE[_key]
        # cross-process disk cache: skip the ~1500-token Claude call entirely when
        # this exact recording was already analysed in an earlier run.
        _dk = llm.cache_key("recanalysis", text[:16000], len(flow or []))
        _disk = llm.cache_get("recanalysis", _dk)
        if _disk is not None:
            _AI_ANALYSIS_CACHE[_key] = _disk
            return _disk
        # Compact, relevant context: the Selenium action lines (typed inputs +
        # navigation) and the deterministic HTTP method+path list. Keeps the giant
        # analytics URLs out so the model sees the real journey.
        sel = [ln.strip() for ln in text.splitlines()
               if re.search(r"By(?:ID|Name|CSS|XPath|LinkText)\(", ln)][:150]
        http = ["%s %s" % (s.get("method"), (s.get("path") or "")[:120])
                for s in (flow or [])[:80]]
        ctx = ("SELENIUM ACTIONS:\n" + "\n".join(sel) +
               "\n\nHTTP REQUESTS:\n" + "\n".join(http))[:12000]
        prompt = (
            "Analyze this recorded e-commerce user journey (BlazeMeter, may combine "
            "Selenium UI actions and HTTP requests) so a load-test generator can "
            "parameterize it correctly. Credentials/search/address are often typed "
            "via Selenium type actions, NOT sent as HTTP bodies.\n\n" + ctx +
            "\n\nReturn ONLY JSON:\n"
            '{"platform":"magento|shopify|salesforce_commerce|sap_commerce|bigcommerce|oracle_commerce|other",'
            '"journey":["ordered business steps e.g. Login, Search, PDP, Add to cart, Cart, Shipping, Payment, Order"],'
            '"inputs":[{"field":"username|password|search_keyword|product_id|firstname|lastname|street|city|postcode|region|country_id|telephone|card_number|card_cvv|coupon|qty|<other>","value":"the recorded value","secret":true}],'
            '"warnings":["anything that could break scripting, e.g. login only via Selenium so credentials must come from CSV; hosted payment gateway cannot be replayed; CAPTCHA present; terms & conditions required"],'
            '"notes":"one-line summary of the journey"}')
        out = llm.json_call(prompt, max_tokens=1500)
        if not isinstance(out, dict):
            return {}
        inputs = []
        for it in (out.get("inputs") or [])[:40]:
            if isinstance(it, dict) and it.get("field"):
                inputs.append({"field": str(it["field"])[:40],
                               "value": str(it.get("value", ""))[:160],
                               "secret": bool(it.get("secret"))})
        result = {
            "platform": str(out.get("platform", ""))[:40],
            "journey": [str(x)[:60] for x in (out.get("journey") or [])[:20]],
            "inputs": inputs,
            "warnings": [str(x)[:200] for x in (out.get("warnings") or [])[:10]],
            "notes": str(out.get("notes", ""))[:400],
        }
        _AI_ANALYSIS_CACHE[_key] = result
        llm.cache_put("recanalysis", _dk, result)     # persist for later runs
        return result
    except Exception:
        return {}
