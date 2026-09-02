"""The rename kept the data, and did not leave a half-renamed path behind.

Renaming a package is mechanical. Renaming the files it reads is not: a token
costs a real order to create, and the run history is the only record of what a
client was told. Both were carried across, and both are checked here against
the failure that actually matters -- coming back to an empty store with nothing
to explain why.

The other trap is the working directory. It is still /var/www/html/apea on
purpose, so a rule that rewrote every `apea/` would have broken it, and a rule
that spared every `apea/` left the PACKAGE path wrong inside it. Five test
files pointed at /var/www/html/apea/apea/... after the first pass.

Run:  ./.venv/bin/python tests_rename.py     # expect FAILURES: 0
"""
import io
import os
import re
import sys
import tempfile

sys.path.insert(0, ".")

FAILURES = []


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


print("the package answers to its new name")
import ltmetrics                                    # noqa: E402
import ltmetrics.server                             # noqa: E402
import ltmetrics.config                             # noqa: E402
from ltmetrics.agents import token_memory as TM     # noqa: E402
from ltmetrics.agents import generator, recommendation  # noqa: E402
check("ltmetrics imports", True)
check("so do the agents", bool(generator) and bool(recommendation))
check("there is no apea package left", not os.path.isdir("apea"))

print()
print("data crossed the rename rather than being abandoned")
check("the token store uses the new name",
      TM._STORE.name == ".ltmetrics-token-memory.json")
check("but knows the old one", TM._LEGACY_STORE.name == ".apea-token-memory.json")
check("and migrates it", callable(getattr(TM, "_migrate_legacy", None)))
check("the history db uses the new name",
      ltmetrics.config.DB_PATH.name == "ltmetrics_history.db")
check("and the old db is migrated too",
      "_LEGACY_DB" in io.open("ltmetrics/config.py", encoding="utf-8").read())

# the migration must be a move, not a copy that leaves two diverging stores
_cfg = io.open("ltmetrics/config.py", encoding="utf-8").read()
_tm = io.open("ltmetrics/agents/token_memory.py", encoding="utf-8").read()
check("migration moves, so there is one store not two",
      ".replace(" in _tm and ".replace(" in _cfg)
check("and never overwrites a store that already exists",
      "if _STORE.exists() or not _LEGACY_STORE.exists()" in _tm)
check("sqlite side files come with it", '"-wal", "-shm"' in _cfg)

print()
print("no half-renamed paths")
SKIP = {".git", ".venv", "node_modules", "__pycache__", "projects", "uploads",
        "ci_export", "enrol-failures", "saved_scripts"}
bad = []
for dirpath, dirnames, filenames in os.walk("."):
    dirnames[:] = [d for d in dirnames if d not in SKIP]
    for fn in filenames:
        if not fn.endswith((".py", ".html", ".md", ".sh", ".yml", ".yaml")):
            continue
        p = os.path.join(dirpath, fn)
        try:
            s = io.open(p, encoding="utf-8").read()
        except Exception:
            continue
        if os.path.basename(p) == os.path.basename(__file__):
            continue        # this file documents the rename, so it says the old name
        # Strip prose. A comment explaining what was renamed is not a leftover,
        # and an earlier version of this check reported four of its own.
        code = re.sub(r"#[^\n]*", "", s)
        code = re.sub(r'"""[\s\S]*?"""', "", code)
        code = re.sub(r"'''[\s\S]*?'''", "", code)
        for m in re.finditer(r"[\w./*-]*apea[\w./*-]*", code, re.I):
            t = m.group(0)
            # The working directory is deliberately unchanged, and the two
            # legacy filenames are the whole point of the migration -- code
            # that no longer knows the old name cannot carry the data across.
            if t.startswith("/var/www/html/apea"):
                continue
            if t in ("apea_history.db", ".apea-token-memory.json"):
                continue
            bad.append("%s: %s" % (p, t))
check("nothing still says apea outside the working directory",
      not bad, "; ".join(bad[:3]))
check("the old expansion is gone",
      "Autonomous Performance Engineering" not in
      io.open("ltmetrics/static/index.html", encoding="utf-8").read())

print()
print("the store still behaves after the move")
_real = dict(TM.recall("https://mcstaging.radwell.eu/uk"))
tmp = tempfile.mkdtemp(prefix="ltm-rename-")
B = "https://rename.invalid/shop"
TM.remember(B, {"probe@x.invalid": "abcd1234"})
check("it can still write", TM.recall(B).get("probe@x.invalid") == "abcd1234")
check("and forget", TM.forget(B, "probe@x.invalid") == 1)
check("the operator's own tokens are untouched",
      dict(TM.recall("https://mcstaging.radwell.eu/uk")) == _real)
print("  (%d real token(s) present)" % len(_real))

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)
