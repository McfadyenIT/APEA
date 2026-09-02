"""LT Metrics Knowledge Layer.

A shared, version-controlled repository of domain knowledge that EVERY agent
consults — instead of each agent (or Claude) re-deriving the same facts run
after run. This is the "heart of LT Metrics": platform rules, a correlation library,
known bugs, deterministic repair rules, reusable flow patterns and the prompt
templates Claude uses.

Files live in `knowledge/rules/*.yaml` (human-editable, reviewable in git). The
loader degrades gracefully: if PyYAML is unavailable it tries a `.json` sibling,
and if nothing loads the KB is simply empty and agents keep their built-in
deterministic behavior. Nothing here is on the load-generation hot path.
"""
from .kb import KnowledgeBase, KB

__all__ = ["KnowledgeBase", "KB"]
