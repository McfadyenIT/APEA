"""The KB must now distinguish the three CyberSource integrations, and the
classifier must pick Secure Acceptance for Radwell's captured traffic."""
import sys
sys.path.insert(0, '/var/www/html/apea')
import yaml
from apea.agents import payment_replay

fails = []


def check(label, got, want):
    ok = got == want
    print("   %-62s %-6s %s" % (label[:62], "OK" if ok else "FAIL",
                                "" if ok else "(got %r want %r)" % (got, want)))
    if not ok:
        fails.append(label)


print("=== YAML parses and the three profiles exist ===")
doc = yaml.safe_load(open("/var/www/html/apea/apea/knowledge/rules/browser_patterns.yaml", encoding="utf-8"))
prof = (doc or {}).get("replay_profiles") or {}
for name in ("cybersource_secure_acceptance", "cybersource_microform_v2",
             "cybersource_flex_api_v2"):
    check("profile present: %s" % name, name in prof, True)

print("\n=== each carries the right replay verdict ===")
check("secure_acceptance = conditional",
      prof.get("cybersource_secure_acceptance", {}).get("replayable"), "conditional")
check("secure_acceptance requires_validation",
      prof.get("cybersource_secure_acceptance", {}).get("requires_validation"), True)
check("microform_v2 = False (browser only)",
      prof.get("cybersource_microform_v2", {}).get("replayable"), False)
check("flex_api_v2 = True",
      prof.get("cybersource_flex_api_v2", {}).get("replayable"), True)

print("\n=== signatures map captured traffic to the right profile ===")
sigs = prof.get("_signatures") or {}
for probe, want in (
        ("https://mcstaging.radwell.eu/uk/pdl_cybs/secureAccept/getParams/",
         "cybersource_secure_acceptance"),
        ("https://testsecureacceptance.cybersource.com/pay",
         "cybersource_secure_acceptance"),
        ("https://apitest.cybersource.com/flex/v2/tokens",
         "cybersource_flex_api_v2"),
        ("https://apitest.cybersource.com/microform/v2/sessions",
         "cybersource_microform_v2")):
    hit = None
    for name, pats in sigs.items():
        if any(str(p).lower() in probe.lower() for p in (pats or [])):
            hit = name
            break
    check("%s -> %s" % (probe.split("/")[2] + probe.split("/")[-1][:18], want),
          hit, want)

print("\n=== classifier still runs on the real capture shape ===")
cap = {"payment_network": [
    {"method": "POST", "url": "https://mcstaging.radwell.eu/uk/pdl_cybs/secureAccept/getParams/",
     "status": 200},
]}
out = payment_replay.classify(cap, detected_gateway="paradoxlabs_cybersource")
print("   verdict:", (out or {}).get("replayable"),
      "| mechanism:", (out or {}).get("mechanism"))
check("classify() returned something", bool(out), True)


"""Safeguards and the validation gate must be present and correct on the
Secure Acceptance profile, and the profile must NOT claim to be proven."""
import sys
import yaml
sys.path.insert(0, '/var/www/html/apea')

doc = yaml.safe_load(open(
    "/var/www/html/apea/apea/knowledge/rules/browser_patterns.yaml",
    encoding="utf-8"))
prof = (doc or {}).get("replay_profiles") or {}
sa = prof.get("cybersource_secure_acceptance") or {}
fails = []


def check(label, got, want):
    ok = got == want
    print("   %-60s %-6s %s" % (label[:60], "OK" if ok else "FAIL",
                                "" if ok else "(got %r)" % (got,)))
    if not ok:
        fails.append(label)


print("=== still unproven — must not claim otherwise ===")
check("replayable is conditional", sa.get("replayable"), "conditional")
check("requires_validation", sa.get("requires_validation"), True)
check("no validated_strategy yet", "validated_strategy" in sa, False)

print("\n=== safeguards ===")
sg = sa.get("safeguards") or {}
check("sandbox only", sg.get("environment"), "sandbox_only")
check("allowed submit host is the TEST endpoint",
      sg.get("allowed_submit_hosts"), ["testsecureacceptance.cybersource.com"])
check("production endpoint forbidden",
      sg.get("forbid_submit_hosts"), ["secureacceptance.cybersource.com"])
for k in ("real_pan_prohibited", "redact_card_number", "redact_cvn"):
    check(k, sg.get(k), True)
check("request bodies not saved", sg.get("save_request_body"), False)

print("\n=== blockers cover freshness and immutability ===")
blob = " ".join(sa.get("blockers") or []).lower()
check("uniqueness of transaction_uuid stated", "unique" in blob, True)
check("immutability of the signed package stated", "immutable" in blob, True)
check("names the signed business fields",
      all(x in blob for x in ("amount", "currency", "reference_number")), True)
check("3DS can still divert to a challenge", "acs challenge" in blob, True)

print("\n=== validation gate + 3DS ===")
vg = (sa.get("validation_gate") or {}).get("required_evidence") or []
check("three pieces of required evidence", len(vg), 3)
check("3ds challenge needs a browser",
      (sa.get("3ds_challenge") or {}).get("browser_required"), True)

print("\n=== the evidence-class rule is written down ===")
raw = open("/var/www/html/apea/apea/knowledge/rules/browser_patterns.yaml",
           encoding="utf-8").read()
check("EVIDENCE CLASSES note present", "EVIDENCE CLASSES" in raw, True)
check("names the JS false-positive trap",
      "ParadoxLabs_CyberSource JS" in raw, True)



print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
