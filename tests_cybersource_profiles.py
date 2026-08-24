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
check("four pieces of required evidence", len(vg), 4)
check("3ds challenge needs a browser",
      (sa.get("3ds_challenge") or {}).get("browser_required"), True)

print("\n=== the evidence-class rule is written down ===")
raw = open("/var/www/html/apea/apea/knowledge/rules/browser_patterns.yaml",
           encoding="utf-8").read()
check("EVIDENCE CLASSES note present", "EVIDENCE CLASSES" in raw, True)
check("names the JS false-positive trap",
      "ParadoxLabs_CyberSource JS" in raw, True)



vg = sa.get("validation_gate") or {}
print("\n=== comparison semantics ===")
check("compare is semantic, not byte-for-byte",
      vg.get("compare"), "semantic_after_form_decode")
check("signed_field_names is order-sensitive",
      vg.get("order_sensitive"), ["signed_field_names"])
check("card fields expected only in the submission",
      vg.get("expected_only_in_submission"), ["card_number", "card_cvn"])

raw = open("/var/www/html/apea/apea/knowledge/rules/browser_patterns.yaml",
           encoding="utf-8").read()
check("the phrase 'byte for byte' is gone as a requirement",
      "match byte for byte" in raw, False)

print("\n=== four gates, in order ===")
ev = vg.get("required_evidence") or []
check("four required evidence items", len(ev), 4)
for i, tag in enumerate(("P0-A1", "P0-A2", "P0-A3", "P0-A4")):
    check("%s present and in position" % tag,
          ev[i].strip().startswith(tag) if i < len(ev) else False, True)
check("A4 is the return leg",
      "return leg" in (ev[3].lower() if len(ev) > 3 else ""), True)

print("\n=== return leg is explained ===")
rl = (vg.get("return_leg") or {}).get("note", "")
check("notes CyberSource signs its response too",
      "signs its response" in rl.lower(), True)
check("distinguishes submitted from completed",
      "submitted" in rl.lower(), True)

print("\n=== still unproven ===")
check("replayable conditional", sa.get("replayable"), "conditional")
check("no validated_strategy", "validated_strategy" in sa, False)

print("\n=== capture checklist: 3 groups covering 4 gates ===")
cc = vg.get("capture_checklist") or []
check("three capture groups", len(cc), 3)
proved = [g for grp in cc for g in (grp.get("proves") or [])]
check("all four gates covered", sorted(set(proved)),
      ["P0-A1", "P0-A2", "P0-A3", "P0-A4"])
check("group 3 covers A3 and A4", cc[2].get("proves") if len(cc) > 2 else None,
      ["P0-A3", "P0-A4"])
check("card values redacted in group 2",
      "REDACT" in (cc[1].get("save", "").upper() if len(cc) > 1 else ""), True)

print("\n=== three result states, not two ===")
check("per-gate states", vg.get("result_states"), ["PASS", "FAIL", "INCONCLUSIVE"])
check("overall states", vg.get("overall_states"),
      ["PROVEN", "NOT_REPLAYABLE", "MORE_EVIDENCE_REQUIRED"])

print("\n=== evidence-class rule is stated generally ===")
check("static-code vs runtime-traffic rule",
      "Static-code evidence identifies a LIKELY INTEGRATION" in raw, True)

print()
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
