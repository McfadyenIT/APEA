"""Parity self-test for the recording-analysis refactor.

`recording.py` was split into five single-responsibility modules
(parser / filter / normalizer / metadata_generator / flow_discovery) with
`recording.py` kept as a thin facade. This test guards that the split is
BEHAVIOR-PRESERVING:

  1. the facade re-exports the exact same function/regex OBJECTS that now live
     in the sub-modules (no accidental shadow copies), and
  2. parsing the real BlazeMeter recordings still yields the same essentials the
     pipeline depends on — a non-empty ordered HTTP flow, the Selenium-typed
     username+password, and harvested checkout agreement_ids — and does so
     DETERMINISTICALLY (parse twice, get the same thing).

Runs with NO server and NO network (the LLM layer degrades to {} without a key,
so results are stable offline). From the project root:

    .venv\\Scripts\\activate.bat        (Windows)   or   source .venv/bin/activate
    python test_recording_parity.py

Exits 0 if every check passes, 1 otherwise.
"""
import re
import sys
from pathlib import Path

from apea.agents import recording, parser, filter as rfilter, normalizer
from apea.agents import metadata_generator as meta, flow_discovery

_fails = 0
ROOT = Path(__file__).resolve().parent


def check(name, cond, detail=""):
    global _fails
    ok = bool(cond)
    if not ok:
        _fails += 1
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else "   << " + str(detail)))
    return ok


def _find(*substrings):
    """Newest uploads/ recording whose name contains all the given substrings."""
    cands = [p for p in (ROOT / "uploads").glob("*.yaml")
             if all(s.lower() in p.name.lower() for s in substrings)]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


print("\n1) Facade re-exports the SAME objects (no shadow copies)")
check("parse_recording is parser.parse_recording",
      recording.parse_recording is parser.parse_recording)
check("merge_into_discovery is flow_discovery.merge_into_discovery",
      recording.merge_into_discovery is flow_discovery.merge_into_discovery)
check("_derive_journey is flow_discovery._derive_journey",
      recording._derive_journey is flow_discovery._derive_journey)
check("_extract_selenium_inputs is metadata_generator._extract_selenium_inputs",
      recording._extract_selenium_inputs is meta._extract_selenium_inputs)
check("_extract_agreement_ids is metadata_generator._extract_agreement_ids",
      recording._extract_agreement_ids is meta._extract_agreement_ids)
check("_ASSET_RE is filter._ASSET_RE", recording._ASSET_RE is rfilter._ASSET_RE)
check("_label is normalizer._label", recording._label is normalizer._label)
check("_AI_ANALYSIS_CACHE is the same dict object",
      recording._AI_ANALYSIS_CACHE is meta._AI_ANALYSIS_CACHE)

print("\n2) Filter predicates match the old inline _ASSET_RE / OPTIONS / noise logic")
check("is_asset drops .css", rfilter.is_asset("/static/app.css"))
check("is_asset keeps a REST call", not rfilter.is_asset("/uk/rest/uk/V1/carts/mine"))
check("is_ignored_method drops OPTIONS", rfilter.is_ignored_method("options"))
check("is_instrumentation_noise catches webdriverdetected",
      rfilter.is_instrumentation_noise('{"webDriverDetected":true}'))

print("\n3) Real combined recordings still parse to the same essentials")
for label, needle in (("Amneal", ("Amneal",)), ("Radwell", ("Radwell",))):
    rec_path = _find(*needle)
    if not rec_path:
        check("%s recording present in uploads/" % label, False, "none found — skipping")
        continue
    res = recording.parse_recording(rec_path)
    flow = res.get("flow") or []
    fields = {si.get("field") for si in (res.get("selenium_inputs") or [])}
    check("%s: ordered HTTP flow extracted" % label, len(flow) > 0, res.get("error"))
    check("%s: base_url resolved" % label, bool(res.get("base_url")), res.get("base_url"))
    check("%s: username captured from Selenium" % label, "username" in fields, sorted(fields))
    check("%s: password captured from Selenium" % label, "password" in fields, sorted(fields))
    check("%s: agreement_ids is a list" % label,
          isinstance(res.get("agreement_ids"), list), res.get("agreement_ids"))
    # determinism — parse again, same essentials
    res2 = recording.parse_recording(rec_path)
    same = ([(s.get("method"), s.get("path")) for s in flow]
            == [(s.get("method"), s.get("path")) for s in (res2.get("flow") or [])])
    check("%s: parse is deterministic" % label, same)

print("\n4) Sample fixtures (jmx / har / yaml) parse without error")
samples = ROOT / "blazemeter-recording-analyzer-workspace" / "sample-recordings"
for fn in ("checkout_recording.jmx", "browse_login_search.har", "mixed_workload.yaml"):
    fp = samples / fn
    if not fp.exists():
        check("%s present" % fn, False, "not found — skipping")
        continue
    res = recording.parse_recording(fp)
    check("%s: no fatal parse error" % fn, not res.get("error"), res.get("error"))
    check("%s: endpoints or flow extracted" % fn,
          bool(res.get("endpoints") or res.get("flow")))

print("\n5) API-call grouping (Taurus transactions -> business groups)")
_rad = _find("Radwell")
if not _rad:
    check("Radwell recording present for grouping check", False, "none found")
else:
    res = recording.parse_recording(_rad)
    flow = res.get("flow") or []
    grouped = [s for s in flow if (s.get("group") or "").strip()]
    check("flow steps carry a transaction group", len(grouped) > 0,
          "no step has a 'group' — parser did not capture transaction:")
    groups = recording.api_call_groups(flow)
    check("api_call_groups returns named groups", len(groups) > 0, groups)
    check("group entries have name + count",
          all(g.get("name") and isinstance(g.get("count"), int) and g["count"] > 0
              for g in groups), groups)
    names = {g["name"].lower() for g in groups}
    check("recognizes business transactions (login/cart/checkout/etc.)",
          any(k in " ".join(names) for k in ("login", "cart", "checkout", "search", "home")),
          sorted(names))

print("\n6) Static-endpoint filtering (assets + non-API page GETs)")
check("asset .css is static", rfilter.is_static_request("GET", "/skin/app.css"))
check("image .png is static", rfilter.is_static_request("GET", "/media/logo.png"))
check("html page is static", rfilter.is_static_request("GET", "/about.html"))
check("bare page GET is static", rfilter.is_static_request("GET", "/uk/"))
check("API GET is kept", not rfilter.is_static_request("GET", "/api/magento/route"))
check("REST GET is kept", not rfilter.is_static_request("GET", "/uk/rest/uk/V1/carts/mine"))
check("POST is kept", not rfilter.is_static_request("POST", "/anything"))
check("query GET (AJAX section) is kept",
      not rfilter.is_static_request("GET", "/customer/section/load/?sections=cart"))
check("XHR GET is kept", not rfilter.is_static_request("GET", "/x", xhr=True))
check("asset under an API path is still dropped",
      rfilter.is_static_request("GET", "/api/foo/logo.png?x=1"))
_r2 = _find("Radwell")
if _r2:
    excl = recording.parse_recording(_r2)                       # default: exclude static
    incl = recording.parse_recording(_r2, include_static=True)  # keep everything
    check("parser reports a static_dropped count", "static_dropped" in excl, list(excl)[:8])
    check("include_static=True keeps >= as many steps",
          len(incl.get("flow") or []) >= len(excl.get("flow") or []),
          (len(incl.get("flow") or []), len(excl.get("flow") or [])))
    check("no dropped-asset extensions remain in the default flow",
          not any(re.search(r"\.(css|js|png|jpe?g|gif|svg|woff2?)(\?|$)", (s.get("path") or ""), re.I)
                  for s in (excl.get("flow") or [])))

print("\n" + ("ALL CHECKS PASSED" if _fails == 0 else "%d CHECK(S) FAILED" % _fails))
sys.exit(1 if _fails else 0)
