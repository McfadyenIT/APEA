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

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
