"""Recording noise filtering — decide which recorded requests are real app
traffic worth replaying, and which are static assets / instrumentation noise.

Split out of recording.py (behavior-preserving). These are pure predicates with
no internal dependencies, used by parser.py and flow_discovery.py.
"""
from __future__ import annotations

import re

# Static assets never belong in a load-test flow (images, styles, scripts, fonts,
# source maps, media, documents). Matched on the path (before any query string).
#
# NOTE: .html/.htm are deliberately NOT here. On Magento, SFCC and most commerce
# platforms a ".html" URL is a DYNAMIC catalogue page (product / category) — often
# the most expensive server-side render on the site. Calling it a "static asset"
# mislabelled every product page in the filtered-noise view. Plain-GET pages are
# still excluded from an API-only run by is_static_request(), but as a PAGE, with
# an accurate reason.
_ASSET_RE = re.compile(
    r"\.(png|jpe?g|gif|bmp|svg|webp|ico|css|js|mjs|map|"
    r"woff2?|ttf|eot|otf|mp4|webm|mov|avi|mp3|wav|wasm|pdf|"
    r"scss|less)(\?|$)", re.I)

# Third-party analytics / tag-manager / RUM beacons. These are NOT the system
# under test: replaying them measures somebody else's infrastructure, and on a
# HAR they arrive in bulk. Anchored so a real path like /uk/collections/... is
# never mistaken for the /collect beacon.
_TRACKER_RE = re.compile(
    r"(google-analytics|googletagmanager|google\.com/pagead|doubleclick|"
    r"facebook\.com/tr|connect\.facebook|hotjar|newrelic|nr-data|mixpanel|"
    r"segment\.io|demdex|adobedc|omtrdc|clarity\.ms|optimizely|qualtrics|"
    r"scorecardresearch|quantserve|bat\.bing|snap\.licdn|cdn-cgi/|"
    r"recaptcha/api2|/gtm\.js|/gtag/|/analytics\.js|/__utm\.gif|"
    r"/collect(?:[/?]|$)|/beacon(?:[/?]|$)|/pixel(?:[/?]|$))", re.I)

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

# Framework static mounts. A FILE under one of these is served by the web server
# or CDN, never by the application — whatever its extension, and even when the
# page fetches it by XHR. Magento's Knockout UI templates are the case that
# matters: /static/version…/…/template/summary.html is an .html file fetched via
# XHR, so the extension and XHR rules both said "real call" and it was being
# load-tested. Measuring those measures nginx, not the application.
_STATIC_MOUNT_RE = re.compile(
    r"/(?:pub/)?(?:static|media|assets|_next/static|_nuxt|"
    r"wp-content|wp-includes)/", re.I)
# Only treat a static-mount path as an asset when it actually names a FILE, so a
# real endpoint like POST /media/upload is still load-tested.
_HAS_FILE_EXT_RE = re.compile(r"/[^/?]+\.[a-z0-9]{1,6}(?:\?|$)", re.I)


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


def is_tracker(path: str) -> bool:
    """True for a third-party analytics / tag-manager / RUM beacon. Never the
    system under test, so never worth load-testing."""
    return bool(_TRACKER_RE.search(path or ""))


def is_static_mount_file(path: str) -> bool:
    """True for a FILE under a framework static mount (/static/, /media/,
    /assets/, /_next/static/, wp-content, …). Served by the web server or CDN,
    so load-testing it measures nginx rather than the application. Requires a
    real filename, so POST /media/upload stays a testable endpoint."""
    p = path or ""
    return bool(_STATIC_MOUNT_RE.search(p.split("?")[0]) and _HAS_FILE_EXT_RE.search(p))


def is_static_request(method: str, path: str, xhr: bool = False,
                      json: bool = False) -> bool:
    """True if this request should be EXCLUDED from an API load test: a CORS
    preflight, a tracking beacon, a static asset, or a plain page-navigation GET.
    API/XHR/POST and query-bearing calls are kept — even if they end in
    .json/.xml — unless they are a pure binary/style/script/media asset."""
    # OPTIONS is never replayable. is_ignored_method() existed but nothing called
    # it here, so preflights survived on the JMX and HAR paths.
    if is_ignored_method(method):
        return True
    # Trackers are dropped regardless of method or query string. Previously a
    # beacon like /collect?v=1 counted as "dynamic" and was load-tested, despite
    # the UI promising trackers were filtered out.
    if is_tracker(path):
        return True
    # A file under a framework static mount is served by nginx/CDN, not the app.
    # Checked BEFORE the dynamic rules: Magento's Knockout templates are .html
    # files fetched by XHR, so both of those rules called them a real call.
    if is_static_mount_file(path):
        return True
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
    'filtered noise' view). Assumes the caller already decided it's noise.

    Kept distinct on purpose: lumping dynamic pages in with stylesheets made the
    filtered list unreadable and made the whole filter look untrustworthy."""
    if is_ignored_method(method):
        return "CORS preflight (OPTIONS) — never replayable"
    if is_tracker(path):
        return "third-party tracker / analytics beacon"
    if is_static_mount_file(path):
        return "static file served by the web server / CDN (not the application)"
    if _BINARY_ASSET_RE.search(path or "") or is_asset(path):
        return "static asset (css / js / image / font / media / doc)"
    return "page navigation (dynamic page, excluded from an API-only run)"


def primary_host(urls) -> str:
    """The host under test = the most frequent netloc across a recording. Used to
    drop third-party hosts, which otherwise keep only their PATH and get replayed
    against the target as bogus URLs, inflating the error rate with false 404s."""
    from urllib.parse import urlparse
    counts: dict[str, int] = {}
    for u in urls or []:
        try:
            netloc = urlparse(str(u or "")).netloc
        except Exception:
            netloc = ""
        if netloc:
            counts[netloc] = counts.get(netloc, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def is_instrumentation_noise(body) -> bool:
    """True for Selenium/BlazeMeter instrumentation posts (not real app traffic)."""
    return isinstance(body, str) and "webdriverdetected" in body.lower()


def is_ignored_method(method: str) -> bool:
    """True for HTTP methods we never replay (CORS preflight)."""
    return (method or "").upper() == "OPTIONS"
