# Analysis of checkout_recording.jmx

**Source file:** `sample-recordings\checkout_recording.jmx`
**Test Plan name (as recorded):** "Magento Guest Checkout"
**Thread Group:** "Guest Checkout Users" — 50 users, 60s ramp-up, `on_sample_error = continue`

Analysis method: read the raw JMX XML directly (no BlazeMeter/JMeter GUI, no conversion tooling) and walked the element tree top to bottom — ThreadGroup → HeaderManager → CSVDataSet → each HTTPSamplerProxy / TransactionController / RegexExtractor / JSONPostProcessor / ResponseAssertion — to reconstruct request order, correlation points, and hardcoded values.

---

## 1. Business Flow Reconstructed

The recording captures a single linear user journey through a Magento (Open Source/Adobe Commerce) storefront. Steps in recorded order:

1. **Home Page** — `GET /` on `www.example-shop.com`
   - A `form_key` is scraped out of the HTML via regex (`form_key.*?value="(.+?)"`). This is Magento's CSRF-style token required on nearly every POST form.
2. **Home Page CSS** — `GET` a static CSS asset from `static.example-shop.com`
3. **Google Analytics Beacon** — `GET /collect` on `www.google-analytics.com`
4. *Think Time: 1s*
5. **[Transaction Controller] Login Flow**
   - **Login** — `POST /customer/account/loginPost/` with `login[username]`, `login[password]`, and `form_key`
   - Response Assertion checks the response body contains "dashboard" (login success check)
6. *Think Time: 2s*
7. **Search Product** — `GET /catalogsearch/result/?q=running+shoes`
8. **View Product Detail** — `GET /catalog/product/view/id/12345`
9. **[Transaction Controller] Checkout Flow**
   - **Add to Cart** — `POST /checkout/cart/add/` with `sku`, `qty`, `form_key`
     - `quoteId` extracted from JSON response (`$.quote_id`) via JSONPostProcessor
   - **Apply Coupon** — `POST /checkout/cart/couponPost/` with `coupon_code`
   - **Checkout Shipping** — `POST /rest/V1/carts/${quoteId}/shipping-information` (raw JSON body, address details)
   - **Place Order** — `PUT /rest/V1/carts/${quoteId}/order` (raw JSON body, payment method)
     - Response Assertion checks response body contains "order_id" (order success check)

### Important discrepancy: this is NOT a pure guest checkout

The Test Plan and Thread Group are both labeled "Guest Checkout," but the recorded flow includes an explicit **authenticated login step** (`/customer/account/loginPost/`) with hardcoded credentials before shopping. True Magento guest checkout skips login entirely and instead captures a guest email during the shipping step of the checkout API (`customerEmail` field in `shipping-information` payload). Before building the load test, decide which behavior you actually want to model:

- **Registered-customer checkout** (matches what was recorded) — keep the Login Flow and parameterize credentials.
- **True guest checkout** — remove the Login Flow entirely and add a guest email field to the shipping-information payload instead.

This is the single biggest structural question to resolve before scripting, since it changes both the flow and the data you need to provision (test accounts vs. throwaway guest emails).

---

## 2. What's Already Correlated (good — keep this)

| Value | Extracted from | Extraction method | Used in |
|---|---|---|---|
| `form_key` | Home Page response | RegexExtractor | Login, Add to Cart |
| `quoteId` | Add to Cart response | JSONPostProcessor (`$.quote_id`) | Checkout Shipping, Place Order (path variable) |

Both are dynamic, session-specific values correctly re-injected downstream via JMeter variables — this part of the recording is production-quality and should NOT be replaced with static values.

---

## 3. What Must Be Parameterized Before Load Testing

This is the core deliverable — every hardcoded value below will otherwise cause every one of the 50 virtual users to hammer the exact same account/product/cart, producing invalid results (cache-skewed response times, DB unique-key contention, unrealistic concurrency patterns, or outright failures on the 2nd+ iteration).

| # | Field / Location | Current (recorded) value | Problem | Recommendation |
|---|---|---|---|---|
| 1 | `login[username]` (Login) | `testuser1@example.com` (hardcoded) | Every virtual user logs in as the same account → session collisions, unrealistic cache hits, possible account-lock from concurrent logins | Drive from CSV `username` column (already defined in the CSVDataSet but **not wired up** — see finding below) |
| 2 | `login[password]` (Login) | `Password123!` (hardcoded) | Same as above | Drive from CSV `password` column |
| 3 | `sku` (Add to Cart) | `SKU12345` (hardcoded) | All load hits one product/inventory row; no variability across catalog; unrealistic if SKU has limited stock | Drive from CSV `sku` column, or a data pool of valid, in-stock SKUs |
| 4 | `qty` (Add to Cart) | `1` (hardcoded) | CSV already has a `quantity` column that's unused | Drive from CSV `quantity`, or keep low fixed values but vary occasionally to catch quantity-dependent pricing/tax logic |
| 5 | `q=running+shoes` (Search Product) | hardcoded search term | All 50 threads search the identical term → search cache masks real DB/Elasticsearch load | Parameterize with a list/CSV of representative search terms (mix of high-hit and low-hit terms) |
| 6 | Product ID `12345` (View Product Detail) | hardcoded path segment | Every user views the same PDP; doesn't reflect catalog browsing behavior or exercise different product templates (configurable/bundle/simple) | Parameterize with a product-id pool, ideally correlated from the Search Product results rather than hardcoded |
| 7 | `coupon_code` (Apply Coupon) | `SAVE10` (hardcoded) | CSV defines a `coupon_code` column that is unused; also: not all users should apply a coupon in a realistic mix | Drive from CSV; add a percentage of users who skip the coupon step entirely (use a Throughput/If Controller) |
| 8 | Shipping address (`street`, `city`, `postcode`, `country_id`) in Checkout Shipping JSON body | hardcoded (123 Main St, Springfield, 62704, US) | Static address for every user; if backend does address validation/tax/shipping-rate lookups by region, this hides load on those code paths | Parameterize with a small realistic address pool (varying state/zip at minimum, since Magento shipping/tax rules are often region-based) |
| 9 | `paymentMethod.method` (Place Order) | `checkmo` (hardcoded) | Only exercises the "Check/Money Order" payment path; real traffic mix usually includes credit card, PayPal, etc., each with very different backend cost (gateway calls, tokenization, 3DS) | Parameterize payment method per a realistic traffic-mix distribution if the load test needs to represent true production payment mix |
| 10 | Domains: `www.example-shop.com`, `static.example-shop.com` | hardcoded per-sampler | Fine for hitting one fixed environment, but blocks easy re-use across dev/staging/prod | Externalize as a `HTTPSampler.domain` User Defined Variable / JMeter Property (e.g. `${__P(env.host,www.example-shop.com)}`) so the same script runs against any environment via `-Jenv.host=` |

### Critical gap: the CSV Data Set is defined but not actually used

`CSVDataSet "Test Data"` declares variables `username, password, sku, quantity, coupon_code`, but **none of the samplers reference `${username}`, `${password}`, `${sku}`, `${quantity}`, or `${coupon_code}`** — they all use hardcoded literals instead. This is the most important fix: wire the existing samplers to actually consume the CSV columns that were already planned for. Also confirm/add standard CSVDataSet settings not visible in this excerpt (recycle on EOF, stop thread on EOF, sharing mode = "All threads" vs per-thread) so 50 concurrent users don't collide on the same CSV row.

---

## 4. Other Issues to Resolve Before Scripting a Real Load Test

1. **No Cookie Manager present.** Magento relies on PHP session cookies (and often a `mage-cache-sessid`/`X-Magento-Vary` cookie) to tie the form_key/login/cart together server-side. Nothing in this recording shows an HTTP Cookie Manager. Without one, session state (server-side cart/session affinity) may not be maintained correctly across requests in a real multi-thread run — add a Cookie Manager per-thread ("Clear cookies each iteration" as appropriate).

2. **Non-application traffic is baked into the flow.** The static CSS request (`static.example-shop.com`) and the Google Analytics beacon (`google-analytics.com`) were captured by the browser proxy but generally should be **excluded from a load test**: they don't exercise the system under test, they add noise/variance to results, and hitting a live third-party analytics endpoint (Google) at load is both pointless and potentially against Google's terms. Recommend disabling or deleting both samplers, unless you specifically want to model CDN/static-asset load (in which case, separate them into their own thread group so they don't skew app-server transaction timings).

3. **Missing Content-Type headers on REST calls.** `Checkout Shipping` and `Place Order` send raw JSON bodies (`postBodyRaw=true`) but there's no visible `Content-Type: application/json` header manager scoped to those requests. Magento's REST API will typically reject these without the correct header — verify and add it (either a dedicated HeaderManager on the Checkout Flow controller, or per-request).

4. **Thin assertions.** Both response assertions do plain substring matching on response body text ("dashboard", "order_id"). For load testing, add stronger validation:
   - HTTP response code assertions (2xx) on every sampler
   - JSON-path based assertions on the REST calls (e.g., confirm `order_id` is actually present as a JSON field, not just present as text anywhere in the body)
   - Duration assertions / SLA thresholds per transaction if you want pass/fail gating during the run

5. **No think time inside the Checkout Flow.** Timers exist only between Home Page → Login (1s) and Login → Search (2s). Add to Cart → Apply Coupon → Shipping → Place Order fire back-to-back with zero pacing, which doesn't reflect a real user reading/filling out checkout forms. Add think times (e.g., Gaussian/Uniform Random Timer, 2–5s) between each checkout step for realistic pacing.

6. **No correlation/extraction of `order_id`.** The assertion checks for the string but the value is never captured into a variable. Recommend adding a JSONPostProcessor to extract `order_id` for logging, post-run reconciliation (e.g., verifying order counts against the DB), or cleanup.

7. **Error handling: `on_sample_error = continue`** is reasonable for a load test (don't kill the whole thread on one failed step), but combined with weak assertions above, failed logins/carts could silently cascade (e.g., a failed Add to Cart still lets `Place Order` fire against a stale/absent `quoteId`). Consider adding an "If Controller" to short-circuit downstream checkout steps when `quoteId` extraction fails, so failures don't generate misleading noise/errors deeper in the flow.

8. **Load profile sanity check.** 50 users / 60s ramp-up is a very light smoke-test profile. Confirm this matches intended test type (smoke vs. baseline vs. stress) — for real load testing you'll want this driven by NFR targets (target concurrent users, arrival rate, duration, ramp shape) rather than left at recording defaults.

---

## 5. Summary Checklist Before Building the Load Test

- [ ] Decide: model recorded **registered-customer checkout**, or rebuild as **true guest checkout** (drop Login, add guest email to shipping payload)
- [ ] Wire `${username}` / `${password}` into Login from CSV
- [ ] Wire `${sku}` / `${quantity}` into Add to Cart from CSV
- [ ] Wire `${coupon_code}` into Apply Coupon from CSV; add a "skip coupon" path for a % of users
- [ ] Parameterize search term and product ID (ideally correlate product ID from search results instead of hardcoding)
- [ ] Parameterize/vary shipping address (at least state/zip) to exercise regional tax/shipping logic
- [ ] Decide payment-method mix (checkmo-only vs. representative distribution) and parameterize
- [ ] Externalize `HTTPSampler.domain` values as properties/variables for multi-environment reuse
- [ ] Add HTTP Cookie Manager (per-thread, cleared each iteration)
- [ ] Verify/add `Content-Type: application/json` on the two REST calls
- [ ] Strengthen assertions: response-code checks + JSON-path assertions in addition to substring checks
- [ ] Add think time between all Checkout Flow steps
- [ ] Extract `order_id` into a variable for logging/verification
- [ ] Remove or isolate static asset (CSS) and third-party (Google Analytics) requests from the app-under-test load
- [ ] Confirm/add CSVDataSet sharing mode + recycle settings appropriate for 50+ concurrent threads
- [ ] Confirm load profile (users/ramp/duration) against actual NFR/test-type goals, not just recorder defaults
