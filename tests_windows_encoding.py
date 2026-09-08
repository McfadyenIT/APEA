"""Regression guard: a Windows-written result file must not kill the analysis.

Run after touching executor.py's subprocess launches or the analyzer's file
reads:

    ./.venv/bin/python tests_windows_encoding.py     # expect: FAILURES: 0

A teammate's Windows run on 2026-09-08 placed five real orders, wrote every
result file, and then reported nothing at all:

    'utf-8' codec can't decode byte 0x97 in position 80: invalid start byte

0x97 is an em dash in cp1252. Locust writes locust_failures.csv with the
platform default encoding -- the console code page on Windows, UTF-8 on Linux --
so the same code passed here and failed there. The byte got into the file
because the day before I had put an em dash into the failure message itself.

Three defences, and the test holds all three, because any one alone leaves a
hole:

  1. messages the engine writes stay ASCII
  2. reads of engine-written files tolerate a byte they cannot decode
  3. child processes are launched in UTF-8 mode, so the files are right at source
"""
import csv
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ltmetrics.agents.analyzer import _read_failures, _read_stats   # noqa: E402
from ltmetrics.agents import executor                               # noqa: E402

fails = []


def check(label, ok):
    print("   %-64s %s" % (label[:64], "OK" if ok else "FAIL"))
    if not ok:
        fails.append(label)


def cp1252_run():
    """A run directory whose CSVs were written the way Windows Locust writes them."""
    d = Path(tempfile.mkdtemp(prefix="ltm-cp1252-"))
    (d / "results").mkdir()
    (d / "results" / "locust_failures.csv").write_bytes(
        ("Method,Name,Error,Occurrences\n"
         'POST,"TXN: Checkout","order not placed — stopped at Cart contains '
         'items: cart line price is 0",1\n').encode("cp1252"))
    (d / "results" / "locust_stats.csv").write_bytes(
        ("Type,Name,Request Count,Failure Count,Median Response Time,"
         "Average Response Time,Min Response Time,Max Response Time,"
         "Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,"
         "98%,99%,99.9%,99.99%,100%\n"
         'POST,"Order created — priced",1,0,100,100,100,100,0,0.5,0,'
         "100,100,100,100,100,100,100,100,100,100,100\n").encode("cp1252"))
    return d


print("\n== a Windows-written result file does not kill the analysis ==")

d = cp1252_run()

print("\nreads survive a byte they cannot decode")
try:
    f = _read_failures(d)
    check("the failures file is read rather than raising", bool(f))
    check("and the row still carries its transaction name",
          f and f[0]["name"] == "TXN: Checkout")
except UnicodeDecodeError as e:
    check("the failures file is read rather than raising (%s)" % e, False)

try:
    eps, agg = _read_stats(d)
    check("the stats file is read rather than raising", bool(eps))
    check("and the endpoint's numbers survive", eps and eps[0]["num_requests"] == 1)
except UnicodeDecodeError as e:
    check("the stats file is read rather than raising (%s)" % e, False)

print("\nchild processes are launched in UTF-8 mode")
env = executor._utf8_env()
check("PYTHONUTF8 is set", env.get("PYTHONUTF8") == "1")
check("PYTHONIOENCODING is utf-8", env.get("PYTHONIOENCODING") == "utf-8")
check("the rest of the environment is inherited, not replaced",
      len(env) > 2 and "PATH" in env)

src = io.open(ROOT / "ltmetrics" / "agents" / "executor.py", encoding="utf-8").read()
check("every subprocess launch uses it",
      src.count("env=_utf8_env()") >= 3)

print("\nmessages the engine writes stay ASCII")
gen = io.open(ROOT / "ltmetrics" / "agents" / "generator.py", encoding="utf-8").read()
# Anchored to the format string itself. "order not placed" also appears in a
# comment above the code, and a window around the first match tested that
# comment rather than the message.
check("the reason is built with an ASCII separator",
      " - stopped at %s: %s" in gen)
check("no em-dash version survives anywhere in the file",
      " — stopped at %s: %s" not in gen)
check("the failure still names where it stopped",
      'else "order not placed" + _why' in gen)

print("\nFAILURES: %d" % len(fails))
for f_ in fails:
    print("  - %s" % f_)
sys.exit(1 if fails else 0)
