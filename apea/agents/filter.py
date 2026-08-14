"""Recording noise filtering — decide which recorded requests are real app
traffic worth replaying, and which are static assets / instrumentation noise.

Split out of recording.py (behavior-preserving). These are pure predicates with
no internal dependencies, used by parser.py and flow_discovery.py.
"""
from __future__ import annotations

import re

# Static assets never belong in a load-test flow (images, styles, scripts, fonts,
# source maps, media, documents). Matched on the path (before any query string).
_ASSET_RE = re.compile(
    r"\.(png|jpe?g|gif|bmp|svg|webp|ico|css|js|mjs|map|"
    r"woff2?|ttf|eot|otf|mp4|webm|mov|avi|mp3|wav|wasm|pdf|"
    r"html?|xhtml|scss|less)(\?|$)", re.I)

# A request is "dynamic" (a real API/app call worth load-testing) if it isn't a
# GET, carries an XHR/JSON marker, or its path looks like an API route. Anything
# else that is a plain GET is treated as a static page/navigation.
_API_HINTS = ("/api/", "/rest/", "/graphql", "/v1/", "/ajax", "/rpc",
              "/gateway", "/service", "/webapi")

# Pure binary / style / script / font / media assets — never worth load-testing,
# even when reached via an API-looking path (unlike .json/.xml which CAN be API).
_BINARY_ASSET_RE = re.compile(
    r"\.(png|jpe?g|gif|bmp|svg|webp|ico|css|js|mjs|map|woff2?|ttf|eot|otf|"
    r"mp4|webm|mov|avi|mp3|wav|wasm|pdf|scss|less)(\?|$)", re.I)


def is_asset(path: str) -> bool:
    """True if the path points at a static asset/document (drop it from the flow)."""
    return bool(_ASSET_RE.search(path or ""))


def is_dynamic_request(method: str, path: str, xhr: bool = False,
                       json: bool = False) -> bool:
    """True if this looks like a real API/app call (keep it), False if it looks
    like a static page navigation (a plain GET of an HTML shell)."""
    if (method or "GET").upper() != "GET":
        return True
    if xhr or json:
        return True
    p = (path or "").lower()
    if "?" in p:                       # carries query params -> a real request
        return True
    return any(h in p for h in _API_HINTS)


def is_static_request(method: str, path: str, xhr: bool = False,
                      json: bool = False) -> bool:
    """True if this request should be EXCLUDED from an API load test: a static
    asset/document, or a plain page-navigation GET (an HTML shell). API/XHR/POST
    and query-bearing calls are kept — even if they end in .json/.xml — unless
    they are a pure binary/style/script/media asset."""
    if is_dynamic_request(method, path, xhr, json):
        # a real call: drop only if it's an unmistakable binary/style/media asset
        return bool(_BINARY_ASSET_RE.search(path or ""))
    # plain GET with no API/query markers -> a static page or asset: exclude it
    return True


def classify_call(method: str, path: str, xhr: bool = False,
                  json: bool = False) -> str:
    """A short, human label for a KEPT (real) call, used by the UI to badge it:
    "REST" (Magento/OCC REST), "API" (/api//graphql/etc), a bare verb for
    non-GET writes (POST/PUT/PATCH/DELETE), "XHR" for query/JSON GETs, else "GET"."""
    p = (path or "").lower()
    if "/rest/" in p:
        return "REST"
    if any(h in p for h in _API_HINTS):
        return "API"
    m = (method or "GET").upper()
    if m != "GET":
        return m
    if xhr or json or "?" in p:
        return "XHR"
    return "GET"


def static_reason(method: str, path: str, xhr: bool = False,
                  json: bool = False) -> str:
    """Human reason a request was filtered out of the flow (for the UI's
    'filtered noise' view). Assumes the caller already decided it's noise."""
    if _BINARY_ASSET_RE.search(path or "") or is_asset(path):
        return "static asset (css / js / image / font / media / doc)"
    return "page navigation (HTML shell, no API/query markers)"


def is_instrumentation_noise(body) -> bool:
    """True for Selenium/BlazeMeter instrumentation posts (not real app traffic)."""
    return isinstance(body, str) and "webdriverdetected" in body.lower()


def is_ignored_method(method: str) -> bool:
    """True for HTTP methods we never replay (CORS preflight)."""
    return (method or "").upper() == "OPTIONS"
