"""Endpoint normalization helpers — turn raw recorded requests into the clean,
uniform shapes the generator/executor expect: readable labels, de-duplicated
endpoint lists, and extracted response assertions.

Split out of recording.py (behavior-preserving, verbatim moves). Pure helpers
with no internal dependencies.
"""
from __future__ import annotations

import re


def _label(path: str) -> str:
    seg = [s for s in path.split("?")[0].split("/") if s]
    name = seg[-1] if seg else "root"
    name = re.sub(r"[^A-Za-z0-9]+", " ", name).strip().title()
    return (name or "Request")[:40]


def _dedupe(endpoints: list[dict]) -> list[dict]:
    seen, out = set(), []
    for e in endpoints:
        key = (e["method"], e["path"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _short_label(method: str, path: str) -> str:
    seg = [s for s in path.split("?")[0].split("/") if s]
    tail = "/".join(seg[-2:]) if seg else "root"
    tail = re.sub(r"[^A-Za-z0-9/_-]+", "", tail)[:34] or "root"
    return f"{method} {tail}"


def _yaml_asserts(r: dict) -> list:
    """Extract 'contains' text assertions from a Taurus request, if any."""
    out = []
    for a in r.get("assert", []) or []:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict):
            c = a.get("contains")
            if isinstance(c, list):
                out.extend(str(x) for x in c)
            elif c:
                out.append(str(c))
    return out
