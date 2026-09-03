"""Every call carries the stage it belongs to.

A call only had a stage if the recording wrapped it in a transaction. The
generator's OWN calls -- create the cart, add the item, place the order -- were
never in that table, so on a real 25-order run only 19% of traffic was labelled
and any stage view would have attributed three quarters of the time to nothing.

Two design constraints this file exists to hold, because the product runs
against many clients and not just the one it was proved on:

  * the map lives in the knowledge base's `generic` block, not in code and not
    per client. These are the TOOL's call names -- the template emits "Cart
    created" for every store and every platform -- so a Shopify or SAP project
    gets the same labelling without configuring anything.
  * a group that came from the recording always wins. If a tester named their
    transaction "PDP", the operator sees "PDP". Ours only fills the gaps, so no
    client's vocabulary is overwritten by ours.

And one measurement trap: a `TXN:` row is an aggregate, not a call. Its
duration already contains the requests inside it, so counting it in a per-stage
time sum counts the same milliseconds twice.

Run:  ./.venv/bin/python tests_stage_labels.py     # expect FAILURES: 0
"""
import io
import re
import sys

sys.path.insert(0, ".")

import yaml  # noqa: E402

from ltmetrics.agents.generator import _kb_stage_map  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


SRC = io.open("ltmetrics/agents/generator.py", encoding="utf-8").read()
KBY = yaml.safe_load(io.open("ltmetrics/knowledge/rules/platform_rules.yaml",
                             encoding="utf-8"))

print("the map is data, and it is platform-neutral")
check("it lives in the knowledge base",
      "canonical_stages" in (KBY.get("generic") or {}))
check("not hardcoded in the generator",
      "canonical_stages" not in SRC.replace('rules.get("canonical_stages")', ""))
check("every platform inherits it",
      len(_kb_stage_map("shopify")) == len(_kb_stage_map("magento")))
check("and a platform block may still override",
      'KB.platform_rules(block)' in SRC)

_m = _kb_stage_map("")
print()
print("it covers the calls the generator actually emits")
EMITS = re.findall(r'_rc\("([^"]+)"', SRC)
missing = sorted({n for n in EMITS if n not in _m})
check("every _rc() call name has a stage", not missing, ", ".join(missing[:4]))
for n in ("Cart created", "Items added", "Order created",
          "Set payment information", "Shipping methods available"):
    check("%-28r is labelled" % n, n in _m)

print()
print("the client's own vocabulary wins")
check("recorded groups are applied after ours",
      "name_group.update({s[\"name\"]: s[\"group\"] for s in flow_steps" in SRC)
check("an empty recorded group does not blank a good label",
      'if (s.get("group") or "").strip()' in SRC)
check("the reason is recorded", "their vocabulary, not" in SRC
      or "wins" in SRC)

print()
print("a transaction row is an aggregate, not a call")
check("TXN rows are labelled", 'return "TXN"' in SRC)
check("matched on the prefix the generator writes",
      'n.startswith("TXN: ")' in SRC)
check("the double-count is explained",
      "same milliseconds twice" in SRC)

print()
print("a generated script still compiles")
i = SRC.index("tmpl = r'''")
start = SRC.index("'''", i) + 3
t = SRC[start:SRC.index("'''", start)]
check("the stage helper is in the template", "def _stage_of(name):" in t)
t = t.replace("__NAME_GROUP__", "{'Cart created': 'Cart'}")
t = t.replace("__FORCED_PAYMENT__", "''")
t = t.replace("__PAYMENT_ADDL__", "{}")
t = t.replace("__HOSTED_GATEWAYS__", "['cybersource']")
t = t.replace("__WAIT_TIME__", "between(1, 2)")
t = "\n".join(l for l in t.split("\n")
              if not re.fullmatch(r"__[A-Z0-9_]+__", l.strip()))
t = re.sub(r"__[A-Z0-9_]+__", "0", t)
try:
    compile(t, "generated", "exec")
    check("it compiles", True)
except SyntaxError as exc:
    check("it compiles", False, "line %s: %s" % (exc.lineno, exc.msg))

# Run the TEMPLATE's own _stage_of, not a copy of it written here. An earlier
# version of this file exec'd a hand-written duplicate, which would have passed
# happily while the real one was broken.
_i = t.index("def _stage_of(name):")
_lines = t[_i:].split("\n")
_real = [_lines[0]]
for _l in _lines[1:]:
    # the function ends at the first line back at column 0
    if _l.strip() and not _l[0].isspace():
        break
    _real.append(_l)
_real = "\n".join(_real)
ns = {"_NAME_GROUP": {"Cart created": "Cart"}}
exec(compile(_real, "template", "exec"), ns)
check("the function under test came from the template",
      "_stage_of" in ns and "def _stage_of" in _real)
check("a known call resolves", ns["_stage_of"]("Cart created") == "Cart")
check("a transaction resolves to TXN", ns["_stage_of"]("TXN: Checkout") == "TXN")
check("an unknown call is left blank rather than guessed",
      ns["_stage_of"]("Something else") == "")
check("and None does not raise", ns["_stage_of"](None) == "")

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
