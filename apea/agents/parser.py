"""Recording format parsers — detect JMX / HAR / Taurus-YAML by extension and
turn each into the normalized result dict (endpoints, base_url, ordered flow,
selenium inputs, agreement ids, AI analysis).

Split out of recording.py (behavior-preserving, verbatim moves). This is the
orchestrating layer: it reads the raw file, applies `filter` noise rules,
`normalizer` labels, and pulls `metadata_generator` extras + the `flow_discovery`
LLM fallback. Public entry point is `parse_recording`.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

from .filter import (is_asset, is_ignored_method, is_instrumentation_noise,
                     is_static_request, static_reason)

# Cap on how many filtered-noise items we retain for the UI (count is unbounded).
_DROPPED_CAP = 800
from .normalizer import _dedupe, _label, _short_label, _yaml_asserts
from .metadata_generator import (
    _ai_recording_analysis,
    _extract_agreement_ids,
    _extract_selenium_inputs,
    _extract_selenium_steps,
)
from .flow_discovery import _ai_yaml_flow


def parse_recording(path, include_static: bool = False) -> dict:
    """Detect the format by extension and parse into a normalized result.

    `include_static=False` (default) EXCLUDES static assets (css/js/images/fonts/
    media/documents) and static page-navigation GETs, so the flow is API calls
    only — the right shape for a load test. Pass True to keep everything (e.g. a
    faithful JMeter-style full replay)."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".jmx":
            res = _parse_jmx(p, include_static)
        elif ext == ".har":
            res = _parse_har(p, include_static)
        elif ext in (".yaml", ".yml"):
            res = _parse_yaml(p, include_static)
        else:
            return {"source": ext.lstrip("."), "endpoints": [], "base_url": None,
                    "error": f"Unsupported recording type: {ext}"}
    except Exception as exc:  # never crash the run on a bad file
        return {"source": ext.lstrip("."), "endpoints": [], "base_url": None,
                "error": f"{type(exc).__name__}: {exc}"}
    # Checkout-agreement (T&C) ids are store CONFIG (stable, reusable) recorded in
    # the place-order body. The REST agreements endpoints are often not exposed
    # (404), so harvest the ids from the recording here — recording-first.
    res["agreement_ids"] = _extract_agreement_ids(res.get("flow"))
    return res


# --------------------------------------------------------------------------- #
# JMX (JMeter / BlazeMeter)
# --------------------------------------------------------------------------- #
def _parse_jmx(p: Path, include_static: bool = False) -> dict:
    root = ET.parse(p).getroot()
    base = None

    # HTTP Request Defaults often hold the domain/protocol.
    for cfg in root.iter("ConfigTestElement"):
        props = {sp.get("name"): (sp.text or "") for sp in cfg.findall("stringProp")}
        dom = props.get("HTTPSampler.domain")
        if dom:
            proto = props.get("HTTPSampler.protocol") or "https"
            base = f"{proto}://{dom}"
            break

    endpoints = []
    static_dropped = 0
    dropped = []
    for sampler in root.iter("HTTPSamplerProxy"):
        props = {sp.get("name"): (sp.text or "") for sp in sampler.findall("stringProp")}
        method = (props.get("HTTPSampler.method") or "GET").upper()
        path = props.get("HTTPSampler.path") or "/"
        dom = props.get("HTTPSampler.domain") or ""
        proto = props.get("HTTPSampler.protocol") or "https"
        if path.startswith("http"):
            u = urlparse(path)
            path = u.path + (f"?{u.query}" if u.query else "")
            base = base or f"{u.scheme}://{u.netloc}"
        elif dom and not base:
            base = f"{proto}://{dom}"
        if not path.startswith("/"):
            path = "/" + path
        if not include_static and is_static_request(method, path):
            static_dropped += 1
            if len(dropped) < _DROPPED_CAP:
                dropped.append({"method": method, "path": path,
                                "reason": static_reason(method, path)})
            continue
        name = sampler.get("testname") or _label(path)
        endpoints.append({"method": method, "path": path, "name": name})

    return {"source": "jmx", "endpoints": _dedupe(endpoints), "base_url": base,
            "static_dropped": static_dropped, "dropped": dropped}


# --------------------------------------------------------------------------- #
# HAR
# --------------------------------------------------------------------------- #
def _parse_har(p: Path, include_static: bool = False) -> dict:
    data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    base = None
    endpoints = []
    static_dropped = 0
    dropped = []
    for entry in data.get("log", {}).get("entries", []):
        req = entry.get("request", {})
        url = req.get("url", "")
        method = (req.get("method") or "GET").upper()
        if not url:
            continue
        u = urlparse(url)
        if not base and u.netloc:
            base = f"{u.scheme}://{u.netloc}"
        path = u.path or "/"
        full = path + (f"?{u.query}" if u.query else "")
        if not include_static and is_static_request(method, full):
            static_dropped += 1
            if len(dropped) < _DROPPED_CAP:
                dropped.append({"method": method, "path": full,
                                "reason": static_reason(method, full)})
            continue
        endpoints.append({"method": method, "path": full, "name": _label(path)})
    return {"source": "har", "endpoints": _dedupe(endpoints), "base_url": base,
            "static_dropped": static_dropped, "dropped": dropped}


# --------------------------------------------------------------------------- #
# Taurus / BlazeMeter YAML
# --------------------------------------------------------------------------- #
def _walk_do(items, out, group=None):
    """Recursively collect request dicts from Taurus requests / do / then blocks,
    tagging each with the enclosing `transaction:` name (the business group, e.g.
    "Home page", "Login", "Add to cart") so the flow can be grouped like JMeter's
    Throughput Controllers. The OUTERMOST transaction wins for nested controllers."""
    for r in items or []:
        if isinstance(r, str):
            out.append({"url": r, "method": "GET", "body": None, "group": group})
        elif isinstance(r, dict):
            if r.get("do"):                       # transaction wrapper
                _walk_do(r["do"], out, group or r.get("transaction") or r.get("label"))
            elif r.get("then"):                   # if/then block
                _walk_do(r["then"], out, group)
            elif r.get("url"):
                r["group"] = group
                out.append(r)
            # requests with only "actions" (Selenium) are ignored


# A Selenium action list-item: `- verbBySomething(...)` optionally `: 10s`.
# These frequently contain unquoted CSS/JS selectors with ':' and quotes
# (e.g. [data-bind="i18n: 'Proceed To Payment'"]) that break the YAML parser.
# We only consume the HTTP `do:` steps, so neutralizing action lines is safe.
import re
_SELENIUM_LINE = re.compile(r'^(\s*)-\s*[\'"]?[A-Za-z][A-Za-z0-9_]*\(')


def _load_yaml_resilient(text: str) -> dict:
    """Load Taurus/BlazeMeter YAML, auto-repairing the malformed Selenium action
    lines that BlazeMeter emits unquoted. The user never sees a parse error."""
    import yaml
    try:
        return yaml.safe_load(text) or {}
    except Exception:
        pass
    # Tier 1 — neutralize Selenium action list items (they aren't used anyway).
    fixed = []
    for line in text.splitlines():
        m = _SELENIUM_LINE.match(line)
        fixed.append((m.group(1) + "- selenium_action") if m else line)
    try:
        return yaml.safe_load("\n".join(fixed)) or {}
    except Exception:
        pass
    # Tier 2 — quote any remaining list-item scalar that carries a stray colon.
    fixed2 = []
    for line in fixed:
        m = re.match(r'^(\s*)-\s+(?![\'"])(.*:.*)$', line)
        if m and not re.match(r'^[A-Za-z0-9_.-]+:\s', m.group(2)):
            val = m.group(2).replace('\\', '\\\\').replace('"', '\\"')
            fixed2.append('%s- "%s"' % (m.group(1), val))
        else:
            fixed2.append(line)
    try:
        return yaml.safe_load("\n".join(fixed2)) or {}
    except Exception:
        return {}


def _parse_yaml(p: Path, include_static: bool = False) -> dict:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return {"source": "yaml", "endpoints": [], "base_url": None, "flow": [],
                "error": "PyYAML not installed. Run: pip install pyyaml"}
    text = p.read_text(encoding="utf-8", errors="ignore")
    data = _load_yaml_resilient(text)
    scenarios = data.get("scenarios", {}) or {}

    raw = []
    default_addr = None
    for sc in scenarios.values():
        if not isinstance(sc, dict):
            continue
        default_addr = default_addr or sc.get("default-address") or sc.get("base-url")
        _walk_do(sc.get("requests", []), raw)

    # Determine the primary host (the site under test) = most common netloc.
    hosts = {}
    for r in raw:
        u = urlparse(str(r.get("url", "")))
        if u.netloc:
            hosts[u.netloc] = hosts.get(u.netloc, 0) + 1
    base_host = max(hosts, key=hosts.get) if hosts else None
    base = f"https://{base_host}" if base_host else (
        str(default_addr).rstrip("/") if default_addr else None)

    flow, endpoints = [], []
    static_dropped = 0
    dropped = []

    def _note_drop(m, pth, reason, count_static=False):
        nonlocal static_dropped
        if count_static:
            static_dropped += 1
        if len(dropped) < _DROPPED_CAP:
            dropped.append({"method": m, "path": pth, "reason": reason})

    for r in raw:
        url = str(r.get("url", ""))
        method = (r.get("method") or "GET").upper()
        u = urlparse(url)
        # keep only same-host, real HTTP verbs, non-asset calls
        if base_host and u.netloc and u.netloc != base_host:
            _note_drop(method, url, "third-party host (%s)" % (u.netloc or "?"))
            continue
        if is_ignored_method(method):
            _note_drop(method, (u.path or url), "CORS preflight (OPTIONS)")
            continue
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        if not path.startswith("/"):
            path = "/" + path
        body = r.get("body")
        # drop Selenium/BlazeMeter instrumentation noise (not real app traffic)
        if is_instrumentation_noise(body):
            _note_drop(method, path, "instrumentation / webdriver noise")
            continue
        label = _short_label(method, u.path or "/")
        asserts = _yaml_asserts(r)
        headers = r.get("headers") or {}
        hlow = {str(k).lower(): str(v) for k, v in headers.items()}
        ctype = hlow.get("content-type", "")
        # keep a curated set of recorded headers for faithful (JMeter-style) replay
        hdrs = {str(k): str(v) for k, v in headers.items()
                if str(k).lower() in ("authorization", "content-type",
                                      "x-requested-with", "accept")}
        step = {"method": method, "path": path, "label": label,
                "group": r.get("group") or "",   # enclosing Taurus transaction (business group)
                "body": body if isinstance(body, (dict, str)) else None,
                "asserts": asserts, "hdrs": hdrs,
                "xhr": "x-requested-with" in hlow,
                "json": "application/json" in ctype.lower(),
                "rest": "/rest/" in path}   # contains — Magento REST may be /<store>/rest/...
        # Exclude static assets + static page-navigation GETs unless the caller
        # opts in (e.g. faithful full replay). Uses the recorded XHR/JSON markers
        # so a JSON/XHR GET (a real API call) is kept even without an /api/ path.
        if not include_static and is_static_request(method, path, step["xhr"], step["json"]):
            _note_drop(method, path, static_reason(method, path, step["xhr"], step["json"]),
                       count_static=True)
            continue
        flow.append(step)
        if method == "GET":
            endpoints.append({"method": "GET", "path": path, "name": _label(path)})

    # AI heal: if deterministic parsing couldn't extract a flow (badly malformed
    # recording), let Claude extract the HTTP requests from the raw text.
    if not flow:
        ai_flow = _ai_yaml_flow(text)
        if ai_flow:
            flow = ai_flow
            base = base or next((("https://" + urlparse(s["path"]).netloc)
                                 for s in ai_flow if urlparse(s["path"]).netloc), base)
            for s in ai_flow:
                if s["method"] == "GET":
                    endpoints.append({"method": "GET", "path": s["path"],
                                      "name": _label(s["path"])})

    # Combined JMeter+Selenium recordings carry the credentials + search term in
    # the Selenium `type*` UI actions (not in any HTTP body), so extract them here
    # or they'd be lost — that's what feeds the sample-CSV parameterization.
    selenium_inputs = _extract_selenium_inputs(text)

    # Every recorded browser action (not just the 3 categories above), in order,
    # for the analysis report and for fuller parameterization field detection.
    # Purely additive — never touches `flow`, so the generator/executor are
    # unaffected whether or not a recording has any Selenium content at all.
    ui_steps = _extract_selenium_steps(text)

    # LLM analysis layer — a deeper read of the whole recording (journey, typed
    # inputs, platform, and warnings that would break scripting). It STRENGTHENS
    # the deterministic parse: it can fill in inputs the regex missed and flag
    # risks, but the verified HTTP `flow` above stays authoritative (the LLM never
    # invents endpoints). Degrades to {} with no API key.
    ai_analysis = _ai_recording_analysis(text, flow)
    _seen = {si["field"] for si in selenium_inputs}
    for it in ai_analysis.get("inputs", []):
        if it.get("field") and it["field"] not in _seen:
            selenium_inputs.append({"field": it["field"], "target": it["field"],
                                    "secret": bool(it.get("secret")),
                                    "sample": it.get("value", "")})
            _seen.add(it["field"])

    return {"source": "yaml", "endpoints": _dedupe(endpoints),
            "base_url": base, "flow": flow[:1500],
            "selenium_inputs": selenium_inputs,
            "ui_steps": ui_steps,
            "static_dropped": static_dropped,
            "dropped": dropped,
            "ai_analysis": {k: v for k, v in ai_analysis.items() if k != "inputs"}}
