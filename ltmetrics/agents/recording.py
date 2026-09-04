"""Recording ingestion — parse BlazeMeter/JMeter JMX, HAR, and Taurus YAML.

Turns a recorded script into a list of endpoints and merges them into the
crawl-based discovery result (self-healing): recorded transactions become
grounded Locust tasks so the generated script mirrors the real recording.

This module is now a THIN FACADE. The implementation lives in five
single-responsibility modules that mirror the recording-analysis pipeline:

    parser.py             format detection + JMX/HAR/YAML parsers (entry point)
    filter.py             static-asset / instrumentation-noise predicates
    normalizer.py         labels, de-duplication, response assertions
    metadata_generator.py selenium inputs, agreement ids, LLM analysis
    flow_discovery.py     journey derivation, merge-into-discovery, LLM flow heal

Behavior is unchanged — everything that used to live here is re-exported below,
so existing imports (`from .agents import recording as recording_agent`;
`recording_agent.parse_recording`, `.merge_into_discovery`, `._derive_journey`)
and any code reaching into the private helpers keep working exactly as before.
"""
from __future__ import annotations

# Public API ---------------------------------------------------------------- #
from .parser import parse_recording
from .flow_discovery import (merge_into_discovery, _derive_journey,
                             api_call_groups, stage_of_step,
                             is_every_page_call)

# Internal helpers re-exported for backward compatibility (tests / callers that
# reach into the private surface). Same objects, same behavior — just relocated.
from .filter import _ASSET_RE
from .normalizer import _label, _short_label, _dedupe, _yaml_asserts
from .metadata_generator import (
    _extract_agreement_ids,
    _extract_selenium_inputs,
    _extract_selenium_steps,
    _parse_action_line,
    _ai_recording_analysis,
    _SEL_TYPE_RE,
    _AI_ANALYSIS_CACHE,
    _ACTIONS_KEY_RE,
    _LABEL_KEY_RE,
    _YAML_LIST_ITEM_RE,
    _ACTION_CALL_RE,
)
from .flow_discovery import _ai_yaml_flow
from .parser import (
    _parse_jmx,
    _parse_har,
    _parse_yaml,
    _load_yaml_resilient,
    _walk_do,
    _SELENIUM_LINE,
)

__all__ = [
    "parse_recording",
    "merge_into_discovery",
    "_derive_journey",
    "api_call_groups",
    "stage_of_step",
    "is_every_page_call",
    "_extract_agreement_ids",
    "_extract_selenium_inputs",
    "_extract_selenium_steps",
    "_parse_action_line",
    "_ai_recording_analysis",
    "_ai_yaml_flow",
    "_parse_jmx",
    "_parse_har",
    "_parse_yaml",
    "_load_yaml_resilient",
    "_walk_do",
    "_label",
    "_short_label",
    "_dedupe",
    "_yaml_asserts",
    "_ASSET_RE",
]
