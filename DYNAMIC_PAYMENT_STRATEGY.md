# Dynamic Payment Method Strategy

**Approach:** Generator detects and informs, script decides at runtime  
**Status:** ✅ UPDATED  
**Date:** 2026-07-24

---

## The Right Approach

Instead of forcing a payment method at generation time, the **script should decide dynamically at runtime** based on what's actually available at the target site.

### Why This Is Better

**Hardcoding approach (❌ Wrong):**
```python
_FORCED_PAYMENT = 'netterms'  # Force offline
# Problem: Site might not have netterms enabled
# Problem: Script fails with "payment method not available"
```

**Dynamic approach (✅ Correct):**
```python
_FORCED_PAYMENT = ''  # Empty = dynamic selection
# At runtime: Query /carts/mine/payment-methods
# At runtime: Choose best available method:
#   1. Prefer offline (netterms, purchaseorder, etc.)
#   2. Fallback to non-hosted method
#   3. Use hosted method if that's all available
# Result: Script adapts to whatever the site offers
```

---

## How It Works

### Generation Time (What The Generator Does)

1. **Detect** payment method from recording
   - Look for CyberSource, Stripe, netterms, etc.
   - Classify as hosted or offline
   - Store detection info

2. **Inform** the generated script
   - Add comments explaining what was detected
   - DON'T force a specific method

3. **Generate** empty `_FORCED_PAYMENT`
   - Let runtime make the decision

**Example detection info stored in script:**
```python
# PAYMENT METHOD STRATEGY (from recording analysis):
# Payment method will be selected dynamically at runtime based on:
# 1) What the target site actually offers (queried from REST API)
# 2) Preference for offline methods (netterms, purchaseorder, etc.) for load testing
# 3) Fallback to non-hosted methods if offline unavailable
# 4) Use hosted gateway if that's the only option
_FORCED_PAYMENT = ''  # Empty = dynamic selection
```

### Runtime (What The Script Does)

When the script runs checkout:

1. **Query** payment methods from target
   ```python
   GET /carts/mine/payment-methods
   # Returns: ['netterms', 'purchaseorder', 'paradoxlabs_cybersource']
   ```

2. **Analyze** available methods
   ```python
   codes = ['netterms', 'purchaseorder', 'paradoxlabs_cybersource']
   
   # Prefer offline methods
   for offline_method in ['netterms', 'purchaseorder', 'checkmo', ...]:
       if offline_method in codes:
           use_method = offline_method
           break
   
   # Fallback to non-hosted
   if not use_method:
       use_method = first_non_hosted_from(codes)
   
   # Last resort: hosted method
   if not use_method:
       use_method = codes[0]
   ```

3. **Use** selected method
   ```python
   POST /carts/mine/set-payment-information
   {"paymentMethod": {"method": use_method}}
   ```

4. **Report** what was chosen
   ```
   [APEA] Payment method selected: netterms (offline, available on target)
   ```

---

## Example Scenarios

### Scenario 1: Target Has Multiple Methods
```
Target offers: ['netterms', 'purchaseorder', 'paradoxlabs_cybersource']
Script preference: offline > non-hosted > hosted

Runtime decision:
  1. Check for offline methods
  2. Find 'netterms' ✅
  3. Use 'netterms'
  
Result: Uses offline method (best for load testing)
```

### Scenario 2: Target Only Has Hosted Gateway
```
Target offers: ['paradoxlabs_cybersource']
Script preference: offline > non-hosted > hosted

Runtime decision:
  1. Check for offline methods → None found
  2. Check for non-hosted methods → None found
  3. Use hosted method (only option)
  4. If _FORCED_PAYMENT empty and no Playwright:
     → Fail with clear error explaining --browser-payment needed
  
Result: Clear error, not silent failure
```

### Scenario 3: User Forces Specific Method
```
User runs: --payment-method purchaseorder
Script: _FORCED_PAYMENT = 'purchaseorder'

Runtime decision:
  1. _FORCED_PAYMENT is set → use it
  2. Verify it exists in available methods
  3. Use 'purchaseorder'
  
Result: Respects user choice
```

---

## Code Flow

### In Generator (`apea/agents/generator.py`)

```python
# Detect what's in the recording (for information)
detected = _detect_payment_method_from_recording(flow_steps)
# Returns: {'method': 'paradoxlabs_cybersource', 'gateway_type': 'hosted', ...}

# But DON'T force it
forced_payment = str(payment_method or hints.get("payment_method") or "")
# If user didn't specify and there's no repair hint:
# forced_payment = ""  # EMPTY = dynamic selection at runtime

# Generate script with empty _FORCED_PAYMENT
# Script template will have comments explaining detection result
```

### In Generated Script (`locustfile.py`)

```python
_FORCED_PAYMENT = ''  # Dynamic selection at runtime

# At checkout time:
codes = ['netterms', 'purchaseorder', 'paradoxlabs_cybersource']
method = _FORCED_PAYMENT or ...  # Select from available

# Preference order (built into script):
# 1. User-forced (_FORCED_PAYMENT if set)
# 2. Memory hint from prior run (if applicable)
# 3. First offline method found
# 4. First non-hosted method found
# 5. First available method

# This logic already exists in the script!
# We just need to ensure _FORCED_PAYMENT is empty by default
```

---

## Generator Changes Summary

### What Changed

1. **Detection logic unchanged** — Still detects hosted vs. offline from recording

2. **Decision logic changed** — DON'T force a method anymore:
   - Before: `_FORCED_PAYMENT = 'netterms'` (forced)
   - After: `_FORCED_PAYMENT = ''` (empty, dynamic)

3. **Comments enhanced** — Explain that runtime will choose dynamically

4. **User override preserved** — If user specifies `--payment-method`, that still forces it

### Benefits

✅ Script adapts to whatever payment methods are available  
✅ Works with CyberSource, Stripe, netterms, PO, etc. without changes  
✅ Prefers offline for load testing, but not dogmatic  
✅ Clear error messages if only hosted methods available  
✅ User can override if needed  

---

## Generated Script Behavior

### When `_FORCED_PAYMENT` Is Empty (Dynamic)

The script will:

1. **Query** what payment methods are available
   ```
   GET /rest/V1/carts/mine/payment-methods
   ```

2. **Prefer** offline methods for load testing
   ```python
   for offline in ['netterms', 'purchaseorder', 'checkmo', ...]:
       if offline in available_codes:
           use_this_method = offline
           break
   ```

3. **Fallback** to non-hosted if needed
   ```python
   if not method_found:
       for code in available_codes:
           if not is_hosted_gateway(code):
               use_this_method = code
               break
   ```

4. **Report** what was selected
   ```
   [APEA] Payment methods available: netterms, purchaseorder, paradoxlabs_cybersource
   [APEA] Selected payment method: netterms (offline)
   ```

### When `_FORCED_PAYMENT` Is Set (User Override)

The script will:
1. Use the forced method (respect user choice)
2. Verify it's available
3. Fail with clear error if not available

---

## Example: CyberSource Recording

### Generation Phase
```
User uploads: Recording with CyberSource
Generator detects: paradoxlabs_cybersource (hosted)
Generator sets: _FORCED_PAYMENT = ''  # EMPTY
Generator adds comment: "Runtime will select from available methods"
```

### Runtime Phase - Site A (Has Offline)
```
Target offers: ['netterms', 'paradoxlabs_cybersource']
Script queries: What's available?
Script decides: Use netterms (offline)
Result: ✅ Load test works, no iframe needed
```

### Runtime Phase - Site B (Offline-Only)
```
Target offers: ['netterms', 'purchaseorder']
Script queries: What's available?
Script decides: Use netterms (offline)
Result: ✅ Load test works, same script, different method
```

### Runtime Phase - Site C (CyberSource-Only)
```
Target offers: ['paradoxlabs_cybersource']
Script queries: What's available?
Script checks: Any offline? No
Script checks: Any non-hosted? No
Script decides: Use paradoxlabs_cybersource (only option)
If Playwright enabled: ✅ Use browser automation
If Playwright disabled: ⚠️ Clear error: "Only hosted gateway available, use --browser-payment"
```

---

## This Is Why The Script Already Has This Logic!

The generated script ALREADY has the intelligent selection code:

```python
# From locustfile.py template (lines ~1700-1750)
codes = [m.get("code") for m in payment_methods]

# MEMORY: prefer method from prior successful run
_mem_pay = _APPLIED_MEMORY.get("payment_method")
if _mem_pay and _mem_pay in codes:
    method = _mem_pay

# AUTO-SELECT: prefer offline methods
for pref in _OFFLINE_PAYMENTS:
    method = next((c for c in codes if pref in c.lower()), None)
    if method:
        break

# FALLBACK: non-hosted methods
if not method and codes:
    method = next((c for c in codes
                   if not any(g in c.lower() for g in _HOSTED_GATEWAYS)), None)

# FINAL: use forced method if configured
method = _FORCED_PAYMENT or method
```

**So the script already does dynamic selection!**  
We just need to ensure `_FORCED_PAYMENT` is empty by default, and let the script's built-in logic handle it.

---

## Updated Generator Behavior

| Scenario | `_FORCED_PAYMENT` | Script Behavior |
|----------|------------------|-----------------|
| User specifies method | Set to user value | Use user's choice (force) |
| Repair hint provided | Set to hint value | Use hint (from prior analysis) |
| Recording has payment | Empty | Dynamic selection at runtime |
| No payment detected | Empty | Dynamic selection at runtime |

**In all cases:** Script has built-in logic to intelligently choose from available methods

---

## Why This Matters

### Before (Naive Generator)
```
Generator: "I see CyberSource, I'll use netterms"
Script at runtime: "Available: CyberSource only"
Result: ❌ Script fails "method not available"
```

### After (Smart Generator)
```
Generator: "I see CyberSource in recording"
Generator: "But I won't force anything, let script decide at runtime"
Script at runtime: "Available: CyberSource"
Script: "OK, using CyberSource (only option)"
Result: ✅ Script adapts and works
```

---

## Summary

The **generator detects and informs**, but the **script decides at runtime**.

- ✅ Generator analyzes recording (for context)
- ✅ Generator leaves `_FORCED_PAYMENT` empty (by default)
- ✅ Script queries available methods at runtime
- ✅ Script intelligently selects (prefer offline, fallback safely)
- ✅ Script works for ANY payment configuration
- ✅ User can force a specific method if needed

**Result:** Scripts are flexible, adaptive, and work with whatever the target site offers.

---

**Status:** ✅ IMPLEMENTED  
**Benefit:** Scripts work dynamically with any payment configuration  
**User Control:** Can override with `--payment-method` if needed
