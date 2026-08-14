# Recording Format Reference

Consult this file when `scripts/parse_recording.py` produces an incomplete
or suspicious result and you need to read the raw recording by hand — for
example, an unusual JMX plugin (a third-party sampler type), a HAR that
groups pages unusually, or a Taurus YAML that uses a feature the script
doesn't special-case. For the normal path, trust the script's output and
skip straight to Step 2 of SKILL.md.

## `parsed_recording.json` field reference

```json
{
  "source_file": "path/to/recording.jmx",
  "format": "jmx | har | yaml",
  "platform_guess": "Magento / Adobe Commerce | Salesforce Commerce Cloud | Mirakl | Shopify | Generic",
  "auth_model_guess": "jwt | session_cookie | form_login | unknown",
  "total_requests_parsed": 0,
  "noise_filtered_count": 0,
  "requests": [
    {
      "seq": 1,
      "label": "Login",
      "method": "POST",
      "url": "https://example.com/customer/account/loginPost/",
      "path": "/customer/account/loginPost/",
      "host": "example.com",
      "params": {},
      "page_group": "Checkout Flow",
      "headers": {},
      "body": "...",
      "think_time_ms": 1000,
      "extractors": [{"variable": "form_key", "type": "regex", "expression": "..."}],
      "assertions": [],
      "hit_count": 1,
      "response_status": 200,
      "redirect": false,
      "response_set_cookie": null,
      "response_snippet": "... (HAR only) ...",
      "think_time_capped": false
    }
  ],
  "csv_datasets": [{"filename": "users.csv", "variables": ["username", "password"]}],
  "thread_groups": [{"name": "Checkout Users", "num_threads": "50", "ramp_time_seconds": "60"}],
  "scenarios": [{"name": "browse", "concurrency": 100, "ramp_up_seconds": 60, "hold_for_seconds": 600}],
  "variables_detected": ["email", "password"]
}
```

`path` never has a query string baked into it and `params` is always a
dict — the script splits the URL the same way for all three formats, so
you never need format-specific handling just to read a query parameter.
`response_status`, `redirect`, `response_set_cookie`, and
`response_snippet` are HAR-only (JMX/YAML don't record a response).
`think_time_capped` is `true` when a HAR gap exceeded the 10s clamp — a
signal the real pause may have been longer than what `think_time_ms`
shows, worth a mention in Risk Flags rather than silently treated as
`10000`.

`platform_guess` and `auth_model_guess` are heuristics, not certainties —
sanity-check them against what you actually see in the requests before
stating them as fact in the report.

## JMX (JMeter XML) — raw element map

```
jmeterTestPlan
└── hashTree
    └── TestPlan
        └── hashTree
            ├── ThreadGroup                → one user group / scenario
            │   └── hashTree
            │       ├── HTTPSamplerProxy    → one business request
            │       ├── HeaderManager       → headers for the request(s) that follow
            │       ├── ConfigTestElement   → "HTTP Request Defaults" — domain/protocol
            │       │   (guiclass contains    inherited by samplers that leave their
            │       │   "HttpDefaults")       own domain/protocol blank
            │       ├── CookieManager       → session handling present
            │       ├── CSVDataSet          → declared test-data columns
            │       ├── ConstantTimer /
            │       │   GaussianRandomTimer → think time before next request
            │       ├── ResponseAssertion   → validation the recording tool checked
            │       ├── RegexExtractor /
            │       │   JSONPostProcessor   → a correlation extractor already declared
            │       └── TransactionController → groups samplers under a business-flow label
            └── ResultCollector             → JMeter-only, ignore
```

A recording exported from JMeter's HTTP(S) Test Script Recorder very
commonly sets domain/protocol once via "HTTP Request Defaults" and leaves
every individual sampler's own domain/protocol blank — the script follows
that inheritance (a sampler's own domain/protocol wins if present,
otherwise the nearest enclosing defaults apply). If you ever see a request
with an empty `host` in `parsed_recording.json`, that's the one case the
script couldn't resolve — check the raw JMX for a config element the
script didn't recognize.

Numeric or UUID-looking segments in a path (`\d{4,}` or a UUID pattern) are
almost always a dynamic value — check whether it's the same across every
occurrence of that request (parametrization: comes from static data) or
changes because of something extracted earlier in the same session
(correlation).

Hit counts aren't stored in the JMX itself. If the user also has an
aggregate report / result CSV from the same run, prefer real counts from
that over equal-weighting every request.

A single extractor can capture more than one variable at once
(semicolon-delimited `referenceNames`/`jsonPathExprs` on a
`JSONPostProcessor`) — the script splits these into separate entries in
`extractors[]` rather than storing the raw delimited string as one opaque
name.

## HAR (HTTP Archive) — parsing notes

- Group entries by `pageref`, mapped to the page title in `log.pages[]`.
- Noise filter: static asset extensions and known third-party/analytics
  hosts only. 3xx redirects are **kept**, not treated as noise — a
  redirect can be a trivial bounce, but it can just as easily be a
  meaningful SSO/OAuth hop, and deciding which is a judgment call for you,
  not something to silently discard. Each request carries a `redirect`
  boolean and its real `response_status` so you can make that call; fold
  trivial same-destination bounces into the business flow narrative rather
  than listing them as their own step, and call out anything that looks
  like an auth redirect explicitly.
- Think time = the delta between `startedDateTime` of consecutive *kept*
  (non-noise) entries in the same page, capped at 10s so a recording that
  was left idle doesn't produce an absurd number. When capping actually
  changes the value, `think_time_capped: true` is set — mention it if the
  real gap looks materially longer than 10s (e.g. a user reading a page for
  45s shows up as exactly `10000`ms with the flag set, not silently as a
  plausible-looking 10s pause).
- `postData.text` is the raw body — parse as JSON or form-encoded depending
  on `postData.mimeType`.
- Auth detection: `Authorization: Bearer` → JWT; `Cookie:` containing
  `PHPSESSID`/`JSESSIONID`/`session` → session cookie; a POST to a path
  containing `login` → form login.
- Unlike JMX/YAML (which only define the request side), HAR carries full
  response detail. The script surfaces `response_status`,
  `response_set_cookie`, and a 500-character `response_snippet` per request
  for exactly this reason — scan those snippets for tokens, IDs, or other
  values a later request depends on. That's how you find correlation
  candidates in a HAR that has no pre-declared extractors (a JMX/YAML
  recorded through BlazeMeter's correlation wizard often already has them;
  a HAR pulled from browser devtools almost never does).

## BlazeMeter YAML (Taurus format) — parsing notes

```yaml
execution:
  - scenario: browse
    concurrency: 100
    ramp-up: 60s
    hold-for: 10m

scenarios:
  browse:
    requests:
      - url: https://example.com/api/login
        label: Login
        method: POST
        body:
          email: ${email}
          password: ${password}
        extract-jsonpath:
          token: $.token
        think-time: 1s
```

Taurus's `assert:` block is normally a list of dicts (`{"contains":
"orderId"}`, or `{"contains": ["a","b"], "not": false}`), not the flat
list of strings JMX/HAR produce — the script flattens it to the same
`list[str]` shape everything else uses, so `assertions` never needs a
YAML-specific reading.

`${variable}` placeholders map directly to test-data columns — the variable
name is the column header. `execution[].scenario` links to the matching key
under `scenarios`; if there's more than one `execution` block, the
recording represents more than one distinct user journey (e.g. "browsers"
vs "buyers") and the report should treat them as separate flows, not merge
them.

## Cross-format normalization the script already applies

Deduplicated identical `method+host+path` triples into one request with a
`hit_count` (host is part of the key — the same path on two different
hosts is genuinely two different endpoints); stripped
`Host`/`Content-Length`/`Connection`/`Accept-Encoding` headers (transport
detail a replaying tool manages itself, not a business signal); split every
URL into a clean `path` plus a `params` dict the same way regardless of
source format; clamped HAR think time to a 0–10s range with a
`think_time_capped` flag when clamping actually changed the value; treated
numeric/UUID path segments as dynamic-value signals; classified noise by
matching static-asset extensions against the path and third-party/analytics
signatures against the *host only* (so a business path that happens to
contain a word like "static" isn't misclassified just because of a path
substring match). If you're extracting something by hand because the
script missed it, apply the same rules for consistency with the rest of the
report.

## Known limitations (by design, not bugs)

The script fails loudly rather than guessing on badly malformed input —
non-XML content in a `.jmx` file, a YAML document that isn't a mapping, or
a scenario body that isn't a dict all produce a clear error and a non-zero
exit rather than a best-effort partial parse. If that happens, open the
raw file and check it's actually the format its extension claims.

Deeply nested JMX controllers (many levels of nested Transaction
Controllers) rely on Python's default recursion limit — this is a
non-issue for realistic recordings, but a pathological, machine-generated
JMX with hundreds of nesting levels could hit it. Not worth guarding
against for hand- or tool-recorded traffic.
