"""Knowledge Base loader + query API.

Loads the YAML/JSON rule files under `knowledge/rules/` and exposes a small,
deterministic query surface. All lookups are pure Python (no network, no LLM),
so any agent can call them cheaply and repeatedly.

Query surface:
    KB.magento(key, default)      -> a Magento platform rule
    KB.platform_rules(platform)   -> the whole rule dict for a platform
    KB.correlations()             -> JMeter-style correlation rules
    KB.known_bugs()               -> catalogued bug signatures
    KB.repair_rules()             -> deterministic fixes keyed to error text
    KB.patterns(platform)         -> reusable business-flow patterns
    KB.prompt(name, **fmt)        -> a prompt template (optionally .format()-ed)
    KB.classify_error(text)       -> (bug dict | None)   deterministic, no LLM
    KB.repair_for(text)           -> (repair rule | None) deterministic, no LLM
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

_RULES_DIR = Path(__file__).resolve().parent / "rules"


def _load_file(stem: str) -> Any:
    """Load rules/<stem>.yaml (preferred) or rules/<stem>.json. Never raises."""
    yaml_path = _RULES_DIR / f"{stem}.yaml"
    json_path = _RULES_DIR / f"{stem}.json"
    if yaml_path.exists():
        try:
            import yaml  # PyYAML is already used by the recording parser
            return yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    if json_path.exists():
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _contains_any(haystack: str, needles) -> bool:
    low = (haystack or "").lower()
    return any(str(n).lower() in low for n in (needles or []))


class KnowledgeBase:
    """Lazy-loaded, cached view over the rule files."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    # ---- raw section access ------------------------------------------- #
    def _section(self, name: str) -> Any:
        if name not in self._cache:
            self._cache[name] = _load_file(name)
        return self._cache[name]

    def reload(self) -> None:
        self._cache.clear()

    # ---- platform rules ----------------------------------------------- #
    def platform_rules(self, platform: str = "magento") -> dict:
        data = self._section("platform_rules")
        return (data.get(platform) or {}) if isinstance(data, dict) else {}

    def platforms(self) -> list[str]:
        """All platform ids defined in platform_rules.yaml."""
        data = self._section("platform_rules")
        return list(data.keys()) if isinstance(data, dict) else []

    def magento(self, key: str, default: Any = None) -> Any:
        return self.platform_rules("magento").get(key, default)

    # ---- correlation library ------------------------------------------ #
    def correlations(self) -> list[dict]:
        data = self._section("correlation_library")
        rules = data.get("rules") if isinstance(data, dict) else data
        return rules if isinstance(rules, list) else []

    # ---- known bugs / repair rules ------------------------------------ #
    def known_bugs(self) -> list[dict]:
        data = self._section("known_bugs")
        bugs = data.get("bugs") if isinstance(data, dict) else data
        return bugs if isinstance(bugs, list) else []

    def repair_rules(self) -> list[dict]:
        data = self._section("repair_rules")
        rules = data.get("rules") if isinstance(data, dict) else data
        return rules if isinstance(rules, list) else []

    # ---- patterns ----------------------------------------------------- #
    def patterns(self, platform: Optional[str] = None) -> Any:
        data = self._section("patterns")
        if platform and isinstance(data, dict):
            return data.get(platform) or {}
        return data

    def business_flow(self, platform: str = "magento") -> list[dict]:
        """The Business Flow Model — the ordered checkout states a valid journey
        must pass through. The generated FSM is built FROM this, not hard-coded."""
        p = self.patterns(platform) or {}
        flow = p.get("business_flow") if isinstance(p, dict) else None
        return flow if isinstance(flow, list) else []

    # ---- prompt templates --------------------------------------------- #
    def prompt(self, name: str, **fmt) -> str:
        data = self._section("prompt_templates")
        tmpl = (data.get(name) if isinstance(data, dict) else "") or ""
        if fmt and tmpl:
            try:
                return tmpl.format(**fmt)
            except Exception:
                return tmpl
        return tmpl

    # ---- deterministic classification (no LLM) ------------------------ #
    def classify_error(self, error_text: str) -> Optional[dict]:
        """Return the first known bug whose signature matches the error, else None."""
        for bug in self.known_bugs():
            if _contains_any(error_text, bug.get("signatures")):
                return bug
        return None

    def repair_for(self, error_text: str) -> Optional[dict]:
        """Return the first deterministic repair rule that applies, else None.
        This is what lets a repair happen WITHOUT calling Claude."""
        for rule in self.repair_rules():
            if _contains_any(error_text, rule.get("when")):
                return rule
        return None


# Process-wide singleton every agent imports.
KB = KnowledgeBase()
