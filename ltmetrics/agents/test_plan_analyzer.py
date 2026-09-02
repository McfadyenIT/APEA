"""Test Plan Analyzer — turn a Performance Test Plan document into a machine
contract the rest of LT Metrics can plan from.

This is the first module of LT Metrics's application-aware layer. It reads a Performance
Test Plan (PDF, DOCX, Markdown or plain text) and extracts the plan's INTENT —
objectives, SLA, pass/fail criteria, load profile (users, ramp-up, ramp-down, think
time, duration), business scenarios, target environment, and the set of test types
the plan describes (Smoke / Load / Stress / Spike / Soak). It also reports a
`coverage` map of which sections it actually found, so the UI can show what's known
vs. missing.

IMPORTANT product rule: it does NOT pick or auto-run a test type. It emits ALL test
types found so the UI can present them and the QA engineer chooses which one to run.
Only the chosen type becomes the execution profile.

Platform-independent by construction: it reasons about performance concepts, not any
specific store or tool. AI-assisted (via agents.llm) with a deterministic keyword
fallback, so it degrades gracefully with no API key. Never raises; always returns a
usable contract (at worst the five standard test types with empty configs).

Output contract (test-plan.json):
    {
      "source": "ai" | "heuristic",
      "objectives": [str], "target_environment": str|None,
      "sla": {"p95_ms": num|None, "error_rate_pct": num|None, "throughput_rps": num|None},
      "pass_criteria": [str], "business_scenarios": [str],
      "think_time": {"min_s": num|None, "max_s": num|None},
      "tests": [{"name","type","users","duration","duration_s","ramp_up","ramp_down"}],
      "available_test_types": [str],   # de-duped types from tests[]
      "notes": str
    }
"""
from __future__ import annotations

import re

_TYPES = ("smoke", "load", "stress", "spike", "soak")
_TYPE_ALIASES = {
    "smoke": ("smoke", "sanity", "shakeout"),
    "load": ("load", "baseline", "capacity", "peak", "volume"),
    "stress": ("stress", "breakpoint", "break point", "saturation"),
    "spike": ("spike", "burst", "surge"),
    "soak": ("soak", "endurance", "stability", "longevity", "reliability"),
}


# --------------------------------------------------------------------------- #
# Document text extraction — format-agnostic (PDF / DOCX / Markdown / plain).
# Defensive: uses whichever parser is installed and degrades to '' otherwise.
# --------------------------------------------------------------------------- #
def _extract_pdf(path: str) -> str:
    try:
        import pdfplumber  # type: ignore
        out = []
        with pdfplumber.open(path) as pdf:
            for pg in pdf.pages:
                out.append(pg.extract_text() or "")
        return "\n".join(out)
    except Exception:
        pass
    for mod in ("pypdf", "PyPDF2"):
        try:
            m = __import__(mod)
            reader = m.PdfReader(path)
            return "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception:
            continue
    return ""


def _extract_docx(path: str) -> str:
    """DOCX paragraphs + table cells (test plans often put the load profile in a
    table). Needs python-docx; returns '' if not installed."""
    try:
        import docx  # type: ignore
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs]
        for tbl in d.tables:
            for row in tbl.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    except Exception:
        return ""


def extract_text(pdf_path: str) -> str:
    """Best-effort text extraction from a Performance Test Plan document, dispatched
    by extension: PDF (pdfplumber/pypdf/PyPDF2), DOCX (python-docx), and Markdown /
    plain text (read directly). Returns '' if the file can't be read or the needed
    parser isn't installed. Never raises."""
    if not pdf_path:
        return ""
    ext = str(pdf_path).rsplit(".", 1)[-1].lower() if "." in str(pdf_path) else ""
    if ext in ("md", "markdown", "txt", "text", "rst", "log"):
        try:
            from pathlib import Path as _P
            return _P(pdf_path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    if ext in ("docx", "docm"):
        return _extract_docx(pdf_path)
    return _extract_pdf(pdf_path)   # default: treat as PDF


# --------------------------------------------------------------------------- #
# Duration / number helpers.
# --------------------------------------------------------------------------- #
def _dur_seconds(text):
    """Parse a human duration ('30m', '2 hours', '90 sec', '1h30m') into seconds."""
    if text is None:
        return None
    s = str(text).strip().lower()
    if s.isdigit():
        return int(s)
    total, found = 0, False
    for val, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)", s):
        found = True
        v = float(val)
        if unit.startswith("h"):
            total += v * 3600
        elif unit.startswith("s"):
            total += v
        else:
            total += v * 60
    return int(total) if found else None


def _int(v):
    try:
        return int(round(float(str(v).replace(",", "").strip())))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Deterministic keyword fallback (no API key). Returns the SAME shape the AI
# path does, so every field flows through analyze() regardless of source.
# --------------------------------------------------------------------------- #
_SCENARIO_WORDS = ("login", "sign in", "browse", "search", "product", "pdp",
                   "category", "add to cart", "cart", "checkout", "payment",
                   "order", "registration", "register", "logout", "wishlist",
                   "my account", "home page", "homepage", "user journey",
                   "business flow", "scenario")
_DUR_UNIT = r"(?:h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs)"


def _lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _sentences_with(text, keywords, limit=6):
    """Distinct source lines that mention any keyword (bullets/headings stripped)."""
    out = []
    for ln in _lines(text):
        low = ln.lower()
        if any(k in low for k in keywords):
            s = ln.strip("-*•#>\t ").strip()
            if s and s not in out and len(s) < 300:
                out.append(s)
        if len(out) >= limit:
            break
    return out[:limit]


def _target_env(text):
    m = re.search(r"https?://[^\s)>\"']+", text or "")
    if m:
        return m.group(0)
    for ln in _lines(text):
        low = ln.lower()
        if any(k in low for k in ("environment", "target env", "staging",
                                  "production", "base url", "test env", "endpoint")):
            return ln.strip("-*•#>\t ").strip()[:200]
    return None


def _heuristic(text: str) -> dict:
    low = (text or "").lower()
    tests = []
    for canon, aliases in _TYPE_ALIASES.items():
        pos = min([low.find(a) for a in aliases if a in low] or [-1])
        if pos < 0:
            continue
        # users may precede the type word ("500-user load test"), so look a little
        # behind; but durations/ramps are read FORWARD only, so we don't grab the
        # previous test's numbers.
        window = low[max(0, pos - 40): pos + 260]
        fwd = low[pos: pos + 260]
        users = None
        mu = re.search(r"(\d[\d,]*)\s*(?:concurrent\s*)?(?:virtual\s*)?(?:users|vusers|vus|threads)", window)
        if mu:
            users = _int(mu.group(1))
        # duration: prefer a "for/hold/duration/over N" phrase (avoids grabbing the
        # ramp time), else the first duration-shaped token ahead of the type word.
        dur = None
        md2 = re.search(r"(?:for|hold(?:\s+for)?|duration|over|run(?:ning)?(?:\s+for)?)"
                        r"\D{0,8}(\d+(?:\.\d+)?\s*" + _DUR_UNIT + r")", fwd)
        if md2:
            dur = md2.group(1).strip()
        else:
            md = re.search(r"(\d+(?:\.\d+)?\s*" + _DUR_UNIT + r")", fwd)
            if md:
                dur = md.group(1).strip()
        ramp = None
        mr = re.search(r"ramp[\s-]*up[^0-9]{0,20}(\d+(?:\.\d+)?\s*" + _DUR_UNIT + r")", fwd)
        if mr:
            ramp = mr.group(1).strip()
        rampd = None
        mrd = re.search(r"ramp[\s-]*down[^0-9]{0,20}(\d+(?:\.\d+)?\s*" + _DUR_UNIT + r")", fwd)
        if mrd:
            rampd = mrd.group(1).strip()
        tests.append({"name": canon.capitalize(), "type": canon, "users": users,
                      "duration": dur, "duration_s": _dur_seconds(dur),
                      "ramp_up": ramp, "ramp_down": rampd})
    # think time (document-wide)
    tt = {"min_s": None, "max_s": None}
    mt = re.search(r"think[\s-]*time[^0-9]{0,20}(\d+(?:\.\d+)?)\s*(?:to|-|and)?\s*(\d+(?:\.\d+)?)?\s*(?:s|sec|secs|seconds)", low)
    if mt:
        tt["min_s"] = _int(mt.group(1))
        tt["max_s"] = _int(mt.group(2)) if mt.group(2) else _int(mt.group(1))
    # SLA (p95 / error rate / throughput)
    sla = {"p95_ms": None, "error_rate_pct": None, "throughput_rps": None}
    m95 = re.search(r"(?:p95|95th\s*percentile|response\s*time)[^0-9]{0,20}(\d[\d,]*)\s*(ms|s|sec|seconds)?", low)
    if m95:
        v = _int(m95.group(1))
        sla["p95_ms"] = v * 1000 if (m95.group(2) or "").startswith("s") else v
    me = re.search(r"error\s*rate[^0-9]{0,12}(\d+(?:\.\d+)?)\s*%", low)
    if me:
        try:
            sla["error_rate_pct"] = float(me.group(1))
        except Exception:
            pass
    mtp = re.search(r"(?:throughput|rps|requests?\s*per\s*second)[^0-9]{0,12}(\d[\d,]*)", low)
    if mtp:
        sla["throughput_rps"] = _int(mtp.group(1))
    return {
        "tests": tests, "think_time": tt, "sla": sla,
        "objectives": _sentences_with(text, ("objective", "goal", "purpose", "aim")),
        "pass_criteria": _sentences_with(text, ("pass if", "fail if", "acceptance",
                                                "success criteria", "pass/fail",
                                                "exit criteria", "threshold")),
        "business_scenarios": _sentences_with(text, _SCENARIO_WORDS, limit=8),
        "target_environment": _target_env(text),
    }


# --------------------------------------------------------------------------- #
# AI extraction (structured, cached, guarded).
# --------------------------------------------------------------------------- #
def _ai_extract(text: str) -> dict | None:
    try:
        from . import llm
        if not llm.available() or not text.strip():
            return None
        snippet = text[:12000]
        key = llm.cache_key("testplan", snippet)
        cached = llm.cache_get("testplan", key)
        if cached is not None:
            return cached
        prompt = (
            "Read this Performance Test Plan and extract its intent as STRICT JSON. "
            "Do not invent values that are not in the document; use null when absent. "
            "For EACH performance test the plan describes (smoke/load/stress/spike/soak), "
            "emit an entry with its user count, duration, ramp-up and ramp-down.\n\n"
            'Return ONLY: {"objectives":[str],"target_environment":str|null,'
            '"sla":{"p95_ms":num|null,"error_rate_pct":num|null,"throughput_rps":num|null},'
            '"pass_criteria":[str],"business_scenarios":[str],'
            '"think_time":{"min_s":num|null,"max_s":num|null},'
            '"tests":[{"name":str,"type":"smoke|load|stress|spike|soak",'
            '"users":num|null,"duration":str|null,"ramp_up":str|null,"ramp_down":str|null}]}\n\n'
            "DOCUMENT:\n" + snippet)
        out = llm.json_call(prompt, max_tokens=1600,
                            system="You extract structured performance-test-plan data. "
                                   "Strict JSON only; null for anything not stated.")
        res = out if isinstance(out, dict) else None
        if res is not None:               # don't cache a miss (indistinguishable from empty)
            llm.cache_put("testplan", key, res)
        return res
    except Exception:
        return None


def _norm_type(t):
    t = str(t or "").strip().lower()
    for canon, aliases in _TYPE_ALIASES.items():
        if t == canon or any(t == a or a in t for a in aliases):
            return canon
    return t if t in _TYPES else None


def analyze(pdf_path: str | None = None, text: str | None = None) -> dict:
    """Analyze a Performance Test Plan into the test-plan.json contract. Accepts a
    PDF path or already-extracted text. Never raises."""
    try:
        raw = text if text is not None else (extract_text(pdf_path) if pdf_path else "")
        raw = raw or ""
        ai = _ai_extract(raw)
        # ONE merged shape regardless of source, so objectives / scenarios /
        # environment / pass-criteria flow through even without an API key.
        data = ai if isinstance(ai, dict) else _heuristic(raw)
        source = "ai" if ai else ("heuristic" if raw.strip() else "empty")

        tests = []
        for t in (data.get("tests") or []):
            typ = _norm_type(t.get("type") or t.get("name"))
            if not typ:
                continue
            dur = t.get("duration")
            tests.append({
                "name": t.get("name") or typ.capitalize(),
                "type": typ,
                "users": _int(t.get("users")),
                "duration": dur,
                "duration_s": _dur_seconds(dur),
                "ramp_up": t.get("ramp_up"),
                "ramp_down": t.get("ramp_down"),
            })
        # de-dupe by type (keep the first / most complete)
        seen, uniq = set(), []
        for t in tests:
            if t["type"] in seen:
                continue
            seen.add(t["type"])
            uniq.append(t)
        tests = uniq

        # Guarantee the UI always has selectable types: if the plan yielded none,
        # offer the five standard types with empty configs (the planner fills them).
        if not tests:
            tests = [{"name": t.capitalize(), "type": t, "users": None, "duration": None,
                      "duration_s": None, "ramp_up": None, "ramp_down": None} for t in _TYPES]

        sla = data.get("sla") or {"p95_ms": None, "error_rate_pct": None, "throughput_rps": None}
        tt = data.get("think_time") or {"min_s": None, "max_s": None}
        objectives = data.get("objectives") or []
        pass_criteria = data.get("pass_criteria") or []
        scenarios = data.get("business_scenarios") or []
        env = data.get("target_environment")
        coverage = {                              # which sections were actually found
            "test_types": bool([t for t in tests if any(
                (t.get("users"), t.get("duration_s"), t.get("ramp_up")))]),
            "objectives": bool(objectives),
            "sla": any(v is not None for v in (sla or {}).values()),
            "think_time": any(v is not None for v in (tt or {}).values()),
            "pass_criteria": bool(pass_criteria),
            "business_scenarios": bool(scenarios),
            "target_environment": bool(env),
        }
        return {
            "source": source,
            "objectives": objectives,
            "target_environment": env,
            "sla": sla,
            "pass_criteria": pass_criteria,
            "business_scenarios": scenarios,
            "think_time": tt,
            "tests": tests,
            "available_test_types": [t["type"] for t in tests],
            "coverage": coverage,
            "raw_text_chars": len(raw),
            "notes": ("" if raw.strip() else "No text could be extracted from the "
                      "document (scanned PDF, or the parser for this format isn't "
                      "installed — pip install pypdf / python-docx)."),
        }
    except Exception as exc:
        # Fail-safe: never break the upload flow; return the standard types.
        return {
            "source": "error", "objectives": [], "target_environment": None,
            "sla": {"p95_ms": None, "error_rate_pct": None, "throughput_rps": None},
            "pass_criteria": [], "business_scenarios": [],
            "think_time": {"min_s": None, "max_s": None},
            "tests": [{"name": t.capitalize(), "type": t, "users": None, "duration": None,
                       "duration_s": None, "ramp_up": None, "ramp_down": None} for t in _TYPES],
            "available_test_types": list(_TYPES),
            "raw_text_chars": 0, "notes": "analyzer error: %s" % exc,
        }
