# HAR Analysis: browse_login_search.har

**Source file:** `sample-recordings\browse_login_search.har`
**Recorded:** 2026-07-14, 10:00:00.000Z – 10:00:12.550Z (~12.5s wall clock, 3 pages, 8 HTTP entries)
**Scenario:** Browse home page → log in → search "laptop" → view product → add to cart

---

## 1. What's actually happening, request by request

| # | Time (rel) | Page | Method & URL | Purpose | Notes |
|---|-----------|------|---------------|---------|-------|
| 1 | +0.000s | Home | `GET https://shop.example.com/` | Load home page | 200, HTML |
| 2 | +0.500s | Home | `GET https://static.shop.example.com/assets/main.css` | Static CSS asset | Separate host (likely CDN) |
| 3 | +0.700s | Home | `GET https://www.google-analytics.com/collect?...` | Analytics beacon | Third-party, fire-and-forget |
| 4 | +2.100s | Home | `GET https://shop.example.com/old-home` | Legacy URL | **302 redirect** → `/` |
| 5 | +4.500s | Login | `POST https://shop.example.com/api/login` | Authenticate | Body has plaintext email/password; response sets `SESSIONID` cookie + returns JWT `token` + `customerId` |
| 6 | +7.000s | Search | `GET https://shop.example.com/search/results?q=laptop&sort=relevance` | Product search | Sends `Authorization: Bearer <token>` from step 5; returns SKU `LAP-2001` |
| 7 | +9.200s | Search | `GET https://shop.example.com/product/LAP-2001` | Product detail | SKU came from step 6's response; same bearer token |
| 8 | +12.400s | Search | `POST https://shop.example.com/api/cart/add` | Add to cart | Same bearer token; body `sku=LAP-2001`; response returns a new `cartId` |

This is a fairly standard "browse → auth → search → PDP → add-to-cart" journey. The think-time gaps between steps (2.1s, 2.4s, 2.5s, 2.2s, 3.2s) look like real human pacing and should be preserved/modeled in the load test rather than firing requests back-to-back.

---

## 2. Values that will break (or silently corrupt) a naive replay

### 2.1 Auth token (`token` in login response → `Authorization: Bearer ...` on requests 6, 7, 8) — **CRITICAL**
- Login response body: `"token":"eyJhbGciOiJIUzI1NiJ9.abcdefg"`
- This exact string is hardcoded (as recorded) in the `Authorization` header of the search, product, and cart-add calls.
- **Why it breaks:** JWTs are short-lived and typically tied to the session/user that generated them. Replaying this literal token will fail with 401/403 as soon as it expires, and it *cannot* legitimately authenticate a different virtual user (each VU that logs in with its own credentials gets its own token).
- **Fix:** Correlate — extract `token` from the JSON response of request #5 and inject it into the `Authorization` header of every subsequent request in that user's flow.

### 2.2 Session cookie `SESSIONID` — **CRITICAL**
- Set via `Set-Cookie: SESSIONID=a1b2c3d4e5f6; Path=/; HttpOnly` on the login response.
- Not visible as an outgoing header on later requests in this trimmed HAR, but a browser would attach it automatically on same-origin requests. If the replay tool doesn't manage a cookie jar per virtual user (or hardcodes this value), you'll get session collisions across VUs or 401s once the real session expires/rotates.
- **Fix:** Enable a per-VU cookie manager, or explicitly correlate `SESSIONID` and re-attach it if your test tool doesn't do this automatically.

### 2.3 Login credentials (`email` / `password` in request #5 body) — **CRITICAL**
- `{"email":"jane.doe@example.com","password":"Secret123"}` is a single hardcoded account.
- **Why it breaks:** Running many concurrent virtual users against one real account will produce unrealistic behavior — session/token collisions, possible account lockout from "concurrent login" fraud detection, and it doesn't exercise per-user data paths (cart, wishlist, etc.). It also means every VU's `customerId` and cart are identical, which invalidates result isolation and can pollute shared test data.
- **Fix:** Parameterize with a CSV/data pool of distinct test accounts, one (or a small rotating set) per virtual user thread.
- **Security note:** This HAR contains a plaintext password. Treat it as sensitive — scrub/redact before storing, sharing, or committing this file anywhere.

### 2.4 `customerId` (98213, from login response) — **HIGH**
- Returned in the login response but not observed being reused later in this trace. If later steps of the *full* journey (checkout, order history, profile) use it, it must be treated as dynamic and correlated per logged-in user — hardcoding it will attribute every VU's activity to the same customer account.

### 2.5 SKU `LAP-2001` (search response → product detail → cart add) — **HIGH**
- The product detail request (`/product/LAP-2001`) and the cart-add body (`"sku":"LAP-2001"`) both use the SKU that was returned by the search call (#6).
- **As literally recorded**, this will "work" once, because the search response happens to always return this one item. But this is fragile:
  - It only holds if the catalog/search index doesn't change and "laptop" search consistently ranks this SKU first.
  - The recorded search response contains a single hardcoded result, which is not realistic — real search responses vary by inventory, promotions, and ranking, and under load some SKUs may go out of stock or be discontinued.
- **Fix:** Correlate the SKU dynamically from the search response for each iteration rather than hardcoding `LAP-2001`, so the test remains valid as catalog data changes and so multiple products can be exercised for more realistic cache/DB access patterns.

### 2.6 `cartId` (`cart_9f8e7d`, from cart-add response) — **MEDIUM**
- Generated per cart/session. Not reused within this recording, but any follow-on steps (checkout, view cart, apply coupon) that aren't in this HAR will need this value correlated from the add-to-cart response rather than hardcoded — flag for whoever extends this script further down the funnel.

### 2.7 Third-party analytics beacon (`google-analytics.com/collect`) — **LOW / exclude**
- This is not part of the system under test. Replaying it in a load test (a) wastes load-generator throughput on a third party you don't control, (b) can trip GA rate limiting, and (c) inflates request-count/latency metrics with numbers that say nothing about your app's performance.
- **Fix:** Strip this request from the script, or keep it disabled/excluded from the executed thread group.

### 2.8 Dead redirect `/old-home → 302 → /` — **LOW**
- Adds a request that most real users wouldn't naturally trigger repeatedly (looks like a one-off legacy bookmark/stale link). Replaying it as a fixed step in every iteration isn't wrong per se, but note that the redirect target is absolute — if the test environment's base URL differs from `shop.example.com` (e.g., a staging host), the hardcoded `Location` echoed in the recorded response won't matter for replay (your tool should just follow the `Location` header live), but any assertion hardcoded against `https://shop.example.com/` will fail on a different environment.
- **Fix:** Either drop this step (it doesn't represent real user behavior) or verify redirect target dynamically instead of asserting a literal host.

### 2.9 Static asset `main.css` on `static.shop.example.com` — **LOW**
- Served from a separate host, almost certainly CDN-fronted in production. Load-testing the origin with this in the critical path will misrepresent real-world performance (CDN cache would normally absorb this). Consider excluding static assets from the main scripted transaction, or hitting them through the CDN if you want a "full page weight" test, but track them as a separate, lower-priority transaction/thread group rather than mixing them into your API journey timings.

### 2.10 No visible CSRF/anti-forgery token
- The login flow here goes straight from a redirect landing on `/` to `POST /api/login` — there's no separate GET for a login form page in this trace (page 2 "Login" looks like a client-side/SPA route change, not a fresh page load). If the real app *does* require a CSRF token or anti-forgery cookie minted from an HTML form, this recording doesn't show it and replay may 403 — worth confirming against the actual app before assuming the login POST is a clean, header-only call.

---

## 3. Environment / host considerations
- Two hostnames are in play: `shop.example.com` (app/API) and `static.shop.example.com` (assets), plus the external `www.google-analytics.com`. If this script is pointed at a different environment (staging/perf), only `shop.example.com` and its static subdomain should be re-based — the GA endpoint should stay excluded regardless of target env.

---

## 4. Summary — priority fix list before replay at scale

| Priority | Item | Action |
|---|---|---|
| Critical | Bearer JWT `token` | Extract from login response, inject into headers of steps 6–8 |
| Critical | `SESSIONID` cookie | Use per-VU cookie manager / correlate explicitly |
| Critical | Login email/password | Parameterize from a credentials data pool, one set per VU |
| High | `customerId` | Correlate if used downstream (checkout/profile), don't hardcode |
| High | SKU `LAP-2001` | Correlate from search response; don't assume it's always returned |
| Medium | `cartId` | Correlate from cart-add response for any downstream checkout steps |
| Low | GA beacon | Remove from script — not part of the system under test |
| Low | `/old-home` redirect | Drop, or follow dynamically without hardcoded host assertions |
| Low | `main.css` static asset | Separate from API transaction timings, or exclude if CDN-served in prod |
| Note | Think times (2–3s between steps) | Preserve as pacing/timers rather than zero-wait back-to-back calls |
| Note | Plaintext password in HAR | Redact before storing/sharing this file |
