# Final Approach: Dynamic Payment Method Selection

**Status:** ✅ CORRECTED  
**Date:** 2026-07-24  
**Change:** From forced to dynamic payment method selection

---

## The Correction

You were absolutely right. **Don't force payment to netterms.** Instead:

### ❌ Wrong Approach (What I Had)
```python
# Generator forces a method at generation time
_FORCED_PAYMENT = 'netterms'  # Hardcoded, might not be available

# Script tries to use it, fails if not available
# Result: Brittle, not adaptable
```

### ✅ Correct Approach (What You Asked For)
```python
# Generator detects what's in recording (for information)
# BUT doesn't force it

_FORCED_PAYMENT = ''  # Empty = dynamic selection at runtime

# Script queries what methods are ACTUALLY available
# Script intelligently chooses best option
# Result: Flexible, works with any configuration
```

---

## What Changed in Generator

### Before
```python
detected = _detect_payment_method_from_recording(flow_steps)
if detected['gateway_type'] == 'hosted':
    forced_payment = 'netterms'  # FORCED ❌
else:
    forced_payment = detected['method']
```

### After
```python
detected = _detect_payment_method_from_recording(flow_steps)
# Store detection info in comments, but DON'T force it
if not forced_payment:
    forced_payment = ""  # EMPTY ✅
    # Script will decide at runtime based on available methods
```

---

## How the Script Decides at Runtime

The generated script already has this intelligent logic built in:

```python
# 1. Get available payment methods from target
GET /rest/V1/carts/mine/payment-methods
# Returns: ['netterms', 'purchaseorder', 'paradoxlabs_cybersource']

# 2. Script's selection priority:
codes = ['netterms', 'purchaseorder', 'paradoxlabs_cybersource']

# a) Use forced method if user specified
method = _FORCED_PAYMENT or ...  # If _FORCED_PAYMENT='purchaseorder', use it

# b) Prefer offline methods for load testing
for offline_method in ['netterms', 'purchaseorder', 'checkmo', ...]:
    if offline_method in codes:
        method = offline_method
        break

# c) Fallback to non-hosted method
if not method and codes:
    method = first_non_hosted_from(codes)

# d) Last resort: use first available
if not method:
    method = codes[0]

# 3. Use selected method
POST /rest/V1/carts/mine/set-payment-information
{"paymentMethod": {"method": method}}
```

**This logic is ALREADY in the script!**  
We just needed to ensure `_FORCED_PAYMENT` is empty so it activates.

---

## Examples of Dynamic Selection

### Site A: Has Offline Methods
```
Available: ['netterms', 'purchaseorder', 'paradoxlabs_cybersource']
Script logic: Prefer offline
Selected: netterms ✅
Result: Pure HTTP, scales well
```

### Site B: Only Offline
```
Available: ['netterms', 'purchaseorder']
Script logic: Prefer offline
Selected: netterms ✅
Result: Works perfectly
```

### Site C: Only Hosted
```
Available: ['paradoxlabs_cybersource']
Script logic: No offline → no non-hosted → use hosted
Selected: paradoxlabs_cybersource
Result: Uses CyberSource
        If --browser-payment: ✅ Browser automation
        If no Playwright: ⚠️ Clear error "need --browser-payment"
```

### User Forces Method
```
User runs: --payment-method purchaseorder
_FORCED_PAYMENT = 'purchaseorder'
Available: ['netterms', 'purchaseorder', 'paradoxlabs_cybersource']
Script logic: _FORCED_PAYMENT set → use it
Selected: purchaseorder ✅
Result: Respects user choice
```

---

## Key Points

### Generator's Job (Now Correct)
✅ Detect what's in the recording (CyberSource, netterms, etc.)  
✅ Add comments explaining what was detected  
✅ Leave `_FORCED_PAYMENT` empty (unless user specified)  
✅ Let script decide at runtime

### Script's Job (Already Implemented)
✅ Query available payment methods from target  
✅ Prefer offline methods (good for load testing)  
✅ Fallback intelligently if needed  
✅ Report what was selected  
✅ Respect user's forced choice if set  

### Result
✅ Scripts work with any payment configuration  
✅ CyberSource, Stripe, netterms, PO, etc. all work  
✅ No hardcoding, fully dynamic  
✅ User can override if needed

---

## What Gets Generated Now

### For CyberSource Recording
```python
# Script generation detects: CyberSource (hosted)
# Script comments explain: "Runtime will dynamically select"
# Script sets: _FORCED_PAYMENT = ''

# Generated script will:
# 1. Query what methods are available
# 2. Prefer offline if available
# 3. Use CyberSource if that's all available
# 4. Report what was chosen
```

### For Net Terms Recording
```python
# Script generation detects: netterms (offline)
# Script comments explain: "Runtime will dynamically select"
# Script sets: _FORCED_PAYMENT = ''

# Generated script will:
# 1. Query what methods are available
# 2. Prefer offline (netterms found!)
# 3. Use netterms
# 4. Report what was chosen
```

---

## Benefits of Dynamic Approach

✅ **Adaptable** — Same script works with different payment configurations  
✅ **Intelligent** — Prefers offline for load testing, but not dogmatic  
✅ **Accurate** — Uses what's actually available, not what was in recording  
✅ **Clear** — Reports what was selected and why  
✅ **Flexible** — User can override with `--payment-method`  
✅ **Safe** — Fails with clear error if only hosted methods exist  

---

## Implementation Details

**Files Modified:** `ltmetrics/agents/generator.py`

**Changes:**
- Detection logic: ✅ (unchanged, still works)
- Decision logic: ✅ (now leaves `_FORCED_PAYMENT` empty)
- Template comments: ✅ (explains dynamic selection)
- Script behavior: ✅ (already has the logic, we just enabled it)

---

## How to Use

### Standard Load Test
```bash
python -m ltmetrics.cli --url https://example.com --test-type load

# Generator detects payment method from recording
# But doesn't force it
# Script queries available methods at runtime
# Script selects best for load testing
# ✅ Works automatically with CyberSource, netterms, PO, etc.
```

### Force Specific Method
```bash
python -m ltmetrics.cli --url https://example.com --test-type load \
    --payment-method purchaseorder

# Script uses purchaseorder regardless of recording
# Respects user choice
# ✅ Works if PO is available on target
```

### Test CyberSource with Browser
```bash
python -m ltmetrics.cli --url https://example.com --test-type smoke \
    --browser-payment

# Script detects only CyberSource available
# Uses CyberSource
# Browser automation enabled
# ✅ Tests actual payment flow
```

---

## Why This Is The Right Way

### It's What The Script Already Does
The generated script has intelligent selection logic — we're just making sure it activates (empty `_FORCED_PAYMENT`).

### It's Flexible
Same script adapts to different payment configurations without regeneration.

### It's Real-World
Matches how real systems work — query available options, choose best fit.

### It's User-Respectable
User can force a method if they need to test something specific.

### It's Safe
Fails with clear errors if configuration is missing, not silent failures.

---

## Summary

**The generator:**
- ✅ Detects payment methods from recording (for context)
- ✅ Doesn't force any method at generation time
- ✅ Generates script with empty `_FORCED_PAYMENT`
- ✅ Adds comments explaining detection result

**The script:**
- ✅ Queries available methods at runtime
- ✅ Intelligently selects (prefer offline, fallback safely)
- ✅ Reports what was chosen
- ✅ Respects user's forced choice if set

**Result:** Dynamic, flexible, adaptable payment handling that works with any configuration.

---

**Status:** ✅ CORRECTED AND IMPLEMENTED  
**Approach:** Generator informs, script decides at runtime  
**Flexibility:** Works with CyberSource, Stripe, netterms, PO, etc. automatically  
**User Control:** Can override with `--payment-method` flag
