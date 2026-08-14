#!/usr/bin/env python3
"""
parse_recording.py — deterministic multi-format parser for BlazeMeter recordings.

Why this script exists: walking a JMX's nested hashTree XML, grouping HAR
entries by page, and reading Taurus YAML scenarios are rote, mechanical,
format-specific chores. A script does that reliably every time; it frees the
model to spend its judgment on the part that actually needs it — naming
business flows sensibly and deciding which values are parametrization vs.
correlation candidates.

Usage:
    python parse_recording.py <path-to-recording> [--out parsed_recording.json]

Supports: .jmx (JMeter XML), .har (HTTP Archive JSON), .yaml/.yml (Taurus /
BlazeMeter execution format). Format is auto-detected from content, not just
the extension.

Output: a single normalized JSON document (see references/parsing.md for the
full field reference) that every downstream analysis step reads instead of
re-parsing the raw recording. Every request object has the same shape
regardless of source format: seq, label, method, url, path, host, params,
page_group, headers, body, think_time_ms, extractors, assertions, hit_count.
Format-specific extras (csv_datasets/thread_groups for JMX;
response_status/response_set_cookie/response_snippet for HAR;
variables_detected/scenarios for YAML) are additive, never a reason for a
downstream reader to branch on format.

This script deliberately fails loudly (clear error, exit 1) rather than
silently producing partial or wrong data on malformed input — a load-test
analysis built on quietly-wrong data is worse than one that visibly stopped.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

# ---------------------------------------------------------------------------
# Shared heuristics
# ---------------------------------------------------------------------------

STATIC_ASSET_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|ico|css|js|woff2?|ttf|eot|map|webp|mp4|avif)(\?|$)",
    re.IGNORECASE,
)

# Matched against the host only (see is_noise) — matching against a full URL
# would let a coincidental path substring like "/products/static.html" get
# misclassified as noise just because it contains "static.".
THIRD_PARTY_HOST_RE = re.compile(
    r"(google-analytics\.com|googletagmanager\.com|doubleclick\.net|"
    r"hotjar\.com|fullstory\.com|segment\.io|segment\.com|adobedtm\.com|"
    r"cookiebot\.com|facebook\.net|connect\.facebook|newrelic\.com|"
    r"nr-data\.net|^cdn\.|^static\.|fonts\.googleapis\.com|fonts\.gstatic\.com|"
    r"gstatic\.com|clarity\.ms|bing\.com/bat)",
    re.IGNORECASE,
)

PLATFORM_HINTS = [
    ("Magento / Adobe Commerce", re.compile(r"(customer/account|checkout/cart|catalogsearch|/graphql)", re.IGNORECASE)),
    ("Salesforce Commerce Cloud", re.compile(r"(demandware|dw/shop|/on/demandware\.store)", re.IGNORECASE)),
    ("Mirakl", re.compile(r"mirakl", re.IGNORECASE)),
    ("Shopify", re.compile(r"myshopify\.com", re.IGNORECASE)),
]

VAR_RE = re.compile(r"\$\{(\w+)\}")

# Transport-level headers a replaying tool manages itself — not a business
# signal, and noisy in a report meant to highlight what the application
# actually needs.
TRANSPORT_HEADERS = {"host", "content-length", "connection", "accept-encoding"}


def strip_transport_headers(headers):
    return {k: v for k, v in headers.items() if k.lower() not in TRANSPORT_HEADERS}


def guess_platform(hosts_and_paths):
    text = " ".join(hosts_and_paths)
    for name, pattern in PLATFORM_HINTS:
        if pattern.search(text):
            return name
    return "Generic"


def guess_auth_model(requests):
    # Header values are normally strings (they came from XML/JSON text), but
    # a hand-written YAML recording could set one to `null` — guard with
    # `or ""` everywhere rather than assuming `.get(key, default)` covers it
    # (that default only applies when the key is *absent*, not when it's
    # present with a None value).
    for r in requests:
        headers = {k.lower(): (v or "") for k, v in (r.get("headers") or {}).items()}
        if "authorization" in headers and "bearer" in headers["authorization"].lower():
            return "jwt"
    for r in requests:
        headers = {k.lower(): (v or "") for k, v in (r.get("headers") or {}).items()}
        cookie = headers.get("cookie", "") + headers.get("set-cookie", "")
        if re.search(r"(phpsessid|jsessionid|session)", cookie, re.IGNORECASE):
            return "session_cookie"
    for r in requests:
        if r.get("method") == "POST" and re.search(r"login", r.get("path", "") or "", re.IGNORECASE):
            return "form_login"
    return "unknown"


def is_noise(url):
    """Static assets are detected from the path/extension; third-party noise
    (analytics, CDNs, tag managers) is detected from the host only, so a
    business path that merely contains a word like "static" in its path
    isn't misclassified."""
    parts = urlsplit(url)
    path_for_ext_check = parts.path if parts.path else url
    if STATIC_ASSET_RE.search(path_for_ext_check):
        return True
    if THIRD_PARTY_HOST_RE.search(parts.netloc or ""):
        return True
    return False


def compute_hit_counts(requests):
    # Keyed on (method, host, path) — two different hosts serving the same
    # path are genuinely different endpoints and shouldn't be conflated.
    counts = {}
    for r in requests:
        key = (r["method"], r.get("host", ""), r["path"])
        counts[key] = counts.get(key, 0) + 1
    for r in requests:
        key = (r["method"], r.get("host", ""), r["path"])
        r["hit_count"] = counts[key]
    return requests


def split_url(url):
    """Consistently separate a full URL into (path, query_params) across all
    three formats, so 'path' never has a literal '?...' baked into it and
    'params' is always a dict, even for JMX (which stores query args as
    separate Argument elements, not as HAR/YAML do)."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query)) if parts.query else {}
    return parts.path, params, parts.netloc


# ---------------------------------------------------------------------------
# JMX parsing
# ---------------------------------------------------------------------------

def parse_jmx(path):
    # JMX files pair every component with its own <hashTree> holding that
    # component's children (e.g. a TransactionController's hashTree holds
    # exactly the samplers inside that transaction — nothing more). Walking
    # the tree recursively and threading scope (thread group / transaction /
    # active headers / HTTP request defaults) as parameters — rather than a
    # flat root.iter() with mutable "current_*" globals — is what keeps a
    # request's page_group, headers, and domain/protocol correctly bounded to
    # the scope they actually appear in, instead of leaking into whatever
    # sampler happens to be next in document order.
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        print(f"Could not parse {path} as XML: {e}", file=sys.stderr)
        sys.exit(1)
    root = tree.getroot()

    requests = []
    thread_groups = []
    csv_datasets = []
    state = {"noise_count": 0, "pending_think_time_ms": None}

    def stringprop(el, name):
        node = el.find(f"./stringProp[@name='{name}']")
        return node.text.strip() if node is not None and node.text else ""

    def attach_extractors_and_assertions(hashtree_el, req):
        if hashtree_el is None:
            return
        for child in hashtree_el:
            tag = child.tag
            if tag in ("RegexExtractor", "JSONPostProcessor", "JSONPathExtractor"):
                ref_raw = stringprop(child, "RegexExtractor.refname") or stringprop(child, "JSONPostProcessor.referenceNames")
                expr_raw = (stringprop(child, "RegexExtractor.regex")
                            or stringprop(child, "JSONPostProcessor.jsonPathExprs")
                            or stringprop(child, "JSONPathExtractor.jsonPathExpr"))
                if not ref_raw:
                    continue
                extractor_type = "json_path" if "json" in tag.lower() else "regex"
                # A single extractor can capture several variables at once,
                # semicolon-delimited (e.g. JSONPostProcessor.referenceNames
                # = "token;userId" with matching jsonPathExprs). Split those
                # out into separate extractor entries instead of storing the
                # raw delimited string as one opaque "variable" name.
                names = [n.strip() for n in ref_raw.split(";") if n.strip()]
                exprs = [e.strip() for e in expr_raw.split(";") if e.strip()] if expr_raw else []
                if len(names) > 1 and len(names) == len(exprs):
                    for n, e in zip(names, exprs):
                        req.setdefault("extractors", []).append({"variable": n, "type": extractor_type, "expression": e})
                else:
                    req.setdefault("extractors", []).append({"variable": ref_raw, "type": extractor_type, "expression": expr_raw})

            elif tag == "ResponseAssertion":
                # JMeter's real property name for the assertion strings
                # collection is (yes, misspelled) "Asserion.test_strings".
                # Prefer that explicit collection; fall back to a heuristic
                # scan for older/non-standard exports.
                coll = child.find("./collectionProp[@name='Asserion.test_strings']")
                if coll is not None:
                    test_strings = [n.text for n in coll.findall("./stringProp") if n.text]
                else:
                    test_strings = [
                        n.text for n in child.findall(".//stringProp")
                        if n.text and "Assertion." not in (n.get("name") or "")
                    ]
                if test_strings:
                    req.setdefault("assertions", []).extend(test_strings[:5])

    def walk(hashtree_el, thread_group, transaction, headers, defaults):
        if hashtree_el is None:
            return
        children = list(hashtree_el)
        i = 0
        while i < len(children):
            el = children[i]
            child_hashtree = children[i + 1] if (i + 1 < len(children) and children[i + 1].tag == "hashTree") else None
            tag = el.tag

            if tag == "TestPlan":
                walk(child_hashtree, thread_group, transaction, headers, defaults)

            elif tag == "ThreadGroup":
                tg_name = el.get("testname") or "Thread Group"
                thread_groups.append({
                    "name": tg_name,
                    "num_threads": stringprop(el, "ThreadGroup.num_threads"),
                    "ramp_time_seconds": stringprop(el, "ThreadGroup.ramp_time"),
                })
                walk(child_hashtree, tg_name, None, headers, defaults)

            elif tag == "TransactionController":
                walk(child_hashtree, thread_group, el.get("testname"), headers, defaults)

            elif tag == "ConfigTestElement" and "httpdefaults" in (el.get("guiclass") or "").lower():
                # "HTTP Request Defaults" — domain/protocol set once for a
                # scope, with individual samplers often left blank and
                # expected to inherit these. Without this, every sampler
                # under such a scope would silently get host="" and a
                # schemeless URL.
                new_defaults = dict(defaults)
                d = stringprop(el, "HTTPSampler.domain")
                p = stringprop(el, "HTTPSampler.protocol")
                if d:
                    new_defaults["domain"] = d
                if p:
                    new_defaults["protocol"] = p
                defaults = new_defaults
                walk(child_hashtree, thread_group, transaction, headers, defaults)

            elif tag == "HeaderManager":
                new_headers = dict(headers)
                for coll in el.findall(".//elementProp"):
                    name_node = coll.find("./stringProp[@name='Header.name']")
                    value_node = coll.find("./stringProp[@name='Header.value']")
                    if name_node is not None and value_node is not None:
                        new_headers[name_node.text or ""] = value_node.text or ""
                new_headers = strip_transport_headers(new_headers)
                # Reassign the loop-local `headers` itself (not just the
                # recursive call below) so every *sibling* sampler that
                # follows this HeaderManager in the same scope — not only
                # its own, usually-empty child hashTree — picks up the
                # merged headers.
                headers = new_headers
                walk(child_hashtree, thread_group, transaction, headers, defaults)

            elif tag in ("ConstantTimer", "GaussianRandomTimer", "UniformRandomTimer"):
                delay = stringprop(el, "ConstantTimer.delay") or stringprop(el, "RandomTimer.delay")
                if delay:
                    try:
                        state["pending_think_time_ms"] = int(float(delay))
                    except ValueError:
                        pass
                walk(child_hashtree, thread_group, transaction, headers, defaults)

            elif tag == "CSVDataSet":
                csv_datasets.append({
                    "filename": stringprop(el, "filename"),
                    "variables": [v.strip() for v in stringprop(el, "variableNames").split(",") if v.strip()],
                })
                walk(child_hashtree, thread_group, transaction, headers, defaults)

            elif tag == "HTTPSamplerProxy":
                domain = stringprop(el, "HTTPSampler.domain") or defaults.get("domain", "")
                path = stringprop(el, "HTTPSampler.path")
                method = (stringprop(el, "HTTPSampler.method") or "GET").upper()
                protocol = stringprop(el, "HTTPSampler.protocol") or defaults.get("protocol") or "https"
                url = f"{protocol}://{domain}{path}" if domain else path
                clean_path, query_params, _ = split_url(url)
                label = el.get("testname") or clean_path

                if is_noise(url):
                    # A ConstantTimer immediately before a noise request
                    # (very common: timer, page, then its filtered css/js/
                    # beacon children) must NOT swallow the pending think
                    # time — carry it forward to the next *kept* request
                    # instead of popping it here.
                    state["noise_count"] += 1
                else:
                    think_time_ms = state["pending_think_time_ms"]
                    state["pending_think_time_ms"] = None

                    body_parts = {}
                    post_body_raw = stringprop(el, "HTTPSampler.postBodyRaw") == "true"
                    raw_body_text = None
                    for arg_el in el.findall(".//elementProp[@elementType='HTTPArgument']"):
                        name_node = arg_el.find("./stringProp[@name='Argument.name']")
                        value_node = arg_el.find("./stringProp[@name='Argument.value']")
                        value = value_node.text if value_node is not None and value_node.text else ""
                        if post_body_raw or (name_node is None or not name_node.text):
                            raw_body_text = value
                        else:
                            body_parts[name_node.text] = value

                    if method in ("GET", "HEAD", "DELETE") and body_parts:
                        # JMeter stores query args as Arguments regardless of
                        # method; for these methods they're query params, not
                        # a request body.
                        query_params.update(body_parts)
                        final_body = raw_body_text
                    else:
                        final_body = raw_body_text if raw_body_text is not None else (body_parts or None)

                    req = {
                        "seq": len(requests) + 1,
                        "label": label,
                        "method": method,
                        "url": url,
                        "path": clean_path,
                        "host": domain,
                        "params": query_params,
                        "page_group": transaction or thread_group,
                        "headers": dict(headers),
                        "body": final_body,
                        "think_time_ms": think_time_ms,
                        "extractors": [],
                        "assertions": [],
                    }
                    requests.append(req)
                    attach_extractors_and_assertions(child_hashtree, req)

            else:
                # Unrecognized wrapper (CookieManager, LoopController, etc.) —
                # still descend in case something relevant is nested inside it.
                walk(child_hashtree, thread_group, transaction, headers, defaults)

            i += 2 if child_hashtree is not None else 1

    top_hashtree = root.find("hashTree")
    walk(top_hashtree, None, None, {}, {})

    requests = compute_hit_counts(requests)
    noise_count = state["noise_count"]
    hosts_and_paths = [r["host"] for r in requests] + [r["path"] for r in requests]

    return {
        "format": "jmx",
        "platform_guess": guess_platform(hosts_and_paths),
        "auth_model_guess": guess_auth_model(requests),
        "total_requests_parsed": len(requests),
        "noise_filtered_count": noise_count,
        "requests": requests,
        "csv_datasets": csv_datasets,
        "thread_groups": thread_groups,
        "variables_detected": [],
        "scenarios": [],
    }


# ---------------------------------------------------------------------------
# HAR parsing
# ---------------------------------------------------------------------------

def parse_har(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)

    log = data.get("log", {}) or {}
    pages = {p.get("id"): p.get("title", p.get("id")) for p in (log.get("pages", []) or [])}
    entries = log.get("entries", []) or []

    requests = []
    noise_count = 0
    prev_time = None

    for entry in entries:
        req = entry.get("request", {}) or {}
        resp = entry.get("response", {}) or {}
        url = req.get("url", "")
        method = (req.get("method") or "GET").upper()
        status = resp.get("status", 0)

        # Redirects are NOT automatically treated as noise. A 3xx can be a
        # trivial www->non-www bounce, but it can also be a meaningful
        # SSO/OAuth hop — that's a judgment call for the reasoning layer,
        # not something this deterministic script should silently discard.
        # Only genuine static-asset/third-party noise is dropped here.
        if is_noise(url):
            noise_count += 1
            continue

        clean_path, query_params, host = split_url(url)
        headers = strip_transport_headers({h["name"]: h["value"] for h in (req.get("headers", []) or [])})
        post_data = req.get("postData", {}) or {}
        body = post_data.get("text")

        # HAR uniquely carries full response detail (unlike JMX/YAML, which
        # only define the request side) — surface a snippet of it so
        # correlation candidates (tokens/IDs a response hands back) can be
        # spotted without re-opening the raw HAR file.
        resp_headers = {h["name"]: h["value"] for h in (resp.get("headers", []) or [])}
        set_cookie = resp_headers.get("Set-Cookie") or resp_headers.get("set-cookie")
        resp_content = (resp.get("content", {}) or {}).get("text", "") or ""
        response_snippet = resp_content[:500] if resp_content else None

        started = entry.get("startedDateTime")
        think_time_ms = None
        think_time_capped = False
        if started:
            try:
                t = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                if prev_time is not None:
                    delta_ms = (t - prev_time).total_seconds() * 1000
                    capped_ms = max(0, min(int(delta_ms), 10000))
                    think_time_capped = capped_ms != int(delta_ms)
                    think_time_ms = capped_ms
                prev_time = t
            except (ValueError, TypeError, AttributeError):
                pass

        requests.append({
            "seq": len(requests) + 1,
            "label": pages.get(entry.get("pageref"), clean_path),
            "method": method,
            "url": url,
            "path": clean_path,
            "host": host,
            "page_group": pages.get(entry.get("pageref"), "Ungrouped"),
            "headers": headers,
            "body": body,
            "params": query_params,
            "think_time_ms": think_time_ms,
            "think_time_capped": think_time_capped,
            "extractors": [],
            "assertions": [],
            "response_status": status,
            "redirect": 300 <= status < 400,
            "response_set_cookie": set_cookie,
            "response_snippet": response_snippet,
        })

    requests = compute_hit_counts(requests)
    hosts_and_paths = [r["host"] for r in requests] + [r["path"] for r in requests]

    return {
        "format": "har",
        "platform_guess": guess_platform(hosts_and_paths),
        "auth_model_guess": guess_auth_model(requests),
        "total_requests_parsed": len(requests),
        "noise_filtered_count": noise_count,
        "requests": requests,
        "csv_datasets": [],
        "thread_groups": [],
        "variables_detected": [],
        "scenarios": [],
    }


# ---------------------------------------------------------------------------
# Taurus / BlazeMeter YAML parsing
# ---------------------------------------------------------------------------

def parse_duration(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    units = {"s": 1, "m": 60, "h": 3600}
    if s[-1] in units:
        try:
            return int(float(s[:-1]) * units[s[-1]])
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None


def find_vars(obj, found):
    if isinstance(obj, str):
        found.update(VAR_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            find_vars(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_vars(v, found)


def normalize_yaml_assertions(raw):
    """Taurus's `assert:` block is a list of dicts (e.g. {"contains":
    "orderId"} or {"contains": ["a","b"], "not": false}), not the flat
    list-of-strings that the JMX/HAR parsers produce. Flatten it to the
    same shape everything else uses, so a downstream reader never needs a
    YAML-specific branch just to read an assertion."""
    out = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            val = item.get("contains", item.get("value"))
            if val is None:
                out.append(json.dumps(item, ensure_ascii=False))
            elif isinstance(val, list):
                out.extend(str(v) for v in val)
            else:
                out.append(str(val))
    return out


def parse_yaml(path):
    try:
        import yaml
    except ImportError:
        print(
            "PyYAML is required to parse Taurus/BlazeMeter YAML recordings.\n"
            "Install it with: pip install pyyaml --break-system-packages",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        print(
            f"{path} did not parse into a Taurus/BlazeMeter YAML document "
            "(expected a top-level mapping with 'execution'/'scenarios' keys). "
            "If this is actually a JMX or HAR file, rename it with the correct "
            "extension so format detection picks the right parser.",
            file=sys.stderr,
        )
        sys.exit(1)

    executions = data.get("execution", []) or []
    scenarios_def = data.get("scenarios", {}) or {}
    if not isinstance(scenarios_def, dict):
        scenarios_def = {}

    scenarios_out = []
    for ex in executions:
        if not isinstance(ex, dict):
            continue
        scenarios_out.append({
            "name": ex.get("scenario"),
            "concurrency": ex.get("concurrency"),
            "ramp_up_seconds": parse_duration(ex.get("ramp-up")),
            "hold_for_seconds": parse_duration(ex.get("hold-for")),
        })

    requests = []
    noise_count = 0
    variables_found = set()
    find_vars(data, variables_found)

    for scenario_name, scenario in scenarios_def.items():
        if not isinstance(scenario, dict):
            continue
        for req in (scenario.get("requests", []) or []):
            if not isinstance(req, dict):
                continue
            url = req.get("url", "")
            if is_noise(url):
                noise_count += 1
                continue
            clean_path, query_params, host = split_url(url)
            explicit_params = req.get("params") or {}
            query_params.update(explicit_params)
            think_time_s = parse_duration(req.get("think-time"))
            requests.append({
                "seq": len(requests) + 1,
                "label": req.get("label", clean_path or url),
                "method": (req.get("method") or "GET").upper(),
                "url": url,
                "path": clean_path,
                "host": host,
                "page_group": scenario_name,
                "headers": strip_transport_headers(req.get("headers", {}) or {}),
                "body": req.get("body"),
                "params": query_params,
                "think_time_ms": (think_time_s * 1000) if think_time_s is not None else None,
                "extractors": (
                    [{"variable": k, "type": "json_path", "expression": v} for k, v in (req.get("extract-jsonpath") or {}).items()]
                    + [{"variable": k, "type": "regex", "expression": v} for k, v in (req.get("extract-regexp") or {}).items()]
                ),
                "assertions": normalize_yaml_assertions(req.get("assert")),
            })

    requests = compute_hit_counts(requests)
    hosts_and_paths = [r["host"] for r in requests] + [r["path"] for r in requests]

    return {
        "format": "yaml",
        "platform_guess": guess_platform(hosts_and_paths),
        "auth_model_guess": guess_auth_model(requests),
        "total_requests_parsed": len(requests),
        "noise_filtered_count": noise_count,
        "requests": requests,
        "csv_datasets": [],
        "thread_groups": [],
        "variables_detected": sorted(variables_found),
        "scenarios": scenarios_out,
    }


# ---------------------------------------------------------------------------
# Format detection + entrypoint
# ---------------------------------------------------------------------------

def detect_format(path):
    text_head = ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text_head = f.read(2000)
    except Exception:
        pass

    if path.suffix.lower() == ".jmx" or "jmeterTestPlan" in text_head:
        return "jmx"
    if path.suffix.lower() == ".har":
        return "har"
    if path.suffix.lower() in (".yaml", ".yml"):
        return "yaml"
    stripped = text_head.lstrip()
    if stripped.startswith("{") and '"log"' in text_head:
        return "har"
    if stripped.startswith("<?xml"):
        return "jmx"
    return "yaml"


def main():
    parser = argparse.ArgumentParser(description="Parse a BlazeMeter recording (JMX/HAR/YAML) into a normalized JSON model.")
    parser.add_argument("input", help="Path to the .jmx, .har, or .yaml/.yml recording")
    parser.add_argument("--out", default="parsed_recording.json", help="Output JSON path (default: parsed_recording.json)")
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    fmt = detect_format(path)
    print(f"Detected format: {fmt}", file=sys.stderr)

    try:
        if fmt == "jmx":
            result = parse_jmx(path)
        elif fmt == "har":
            result = parse_har(path)
        else:
            result = parse_yaml(path)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Failed to parse {path} as {fmt.upper()}: {type(e).__name__}: {e}", file=sys.stderr)
        print(
            "This is a clean failure, not silently-wrong output — check "
            "references/parsing.md for the expected structure of this format, "
            "or extract the missing pieces by hand from the raw recording.",
            file=sys.stderr,
        )
        sys.exit(1)

    result["source_file"] = str(path)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(
        f"Parsed {result['total_requests_parsed']} business-relevant requests "
        f"({result['noise_filtered_count']} noise entries filtered). "
        f"Wrote {args.out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
