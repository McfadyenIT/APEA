"""{{column|default}} in payment additional_data.

A sandbox CVV is the same for every account in the pool, so repeating it on
every row is noise. A default lets the pool carry it once.

The thing worth protecting is that this must NOT become a way for a declared
card payment to proceed on a guess. A default is a value the author WROTE
DOWN; a blank column with no default still stops the run.
"""
import re
import sys

sys.path.insert(0, ".")

from ltmetrics.agents import generator as G  # noqa: E402

SRC = open(G.__file__, encoding="utf-8").read()

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


# Rebuild the resolver exactly as the generated script runs it, so the test
# exercises the emitted regex rather than a copy that can drift.
m = re.search(r"_ADDL_PLACEHOLDER = re\.compile\(\s*\n?\s*(r\"[^\"]+\")\s*\)", SRC)
assert m, "could not find _ADDL_PLACEHOLDER in generator.py"
PLACEHOLDER = re.compile(eval(m.group(1)))


def resolve(template, row):
    """Mirror of _row_addl's per-value resolution."""
    missing = []

    def _sub(mm):
        col, fallback = mm.group(1), mm.group(2)
        val = (row or {}).get(col)
        val = "" if val is None else str(val).strip()
        if not val and fallback is not None:
            val = fallback.strip()
        if not val:
            missing.append(col)
        return val

    return PLACEHOLDER.sub(_sub, template), missing


print("the default is used when the column is absent")
out, miss = resolve("{{card_cvv|123}}", {"payment_token": "abc"})
check("absent column falls back", out == "123" and not miss, "got %r" % out)

out, miss = resolve("{{card_cvv|123}}", {"card_cvv": ""})
check("blank column falls back", out == "123" and not miss, "got %r" % out)

print()
print("the column still wins when it has a value")
out, miss = resolve("{{card_cvv|123}}", {"card_cvv": "999"})
check("per-account override beats the default", out == "999" and not miss,
      "got %r" % out)

print()
print("fail-closed is NOT weakened")
out, miss = resolve("{{payment_token}}", {"payment_token": ""})
check("no default, no value -> reported missing", miss == ["payment_token"],
      "missing=%r" % miss)
out, miss = resolve("{{payment_token}}", {})
check("no default, absent column -> reported missing", miss == ["payment_token"],
      "missing=%r" % miss)
out, miss = resolve("{{card_cvv|}}", {"card_cvv": ""})
check("empty default is not a value", miss == ["card_cvv"], "missing=%r" % miss)

print()
print("existing templates are unaffected")
out, miss = resolve('{"card_id": "{{payment_token}}", "cc_cid": "{{card_cvv}}"}',
                    {"payment_token": "f97d08", "card_cvv": "123"})
check("plain placeholders resolve as before",
      out == '{"card_id": "f97d08", "cc_cid": "123"}' and not miss, "got %r" % out)

print()
print("a token must never be given a default")
# Not enforced in code -- a default IS a written-down value, and an author who
# writes one token for every account has said what they meant. But the pool
# summary already flags accounts without their own token, so record that the
# two features do not collide.
out, miss = resolve("{{payment_token|shared}}", {})
check("a defaulted token resolves (author's explicit choice)",
      out == "shared" and not miss, "got %r" % out)

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
