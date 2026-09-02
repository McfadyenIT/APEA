"""Agent Orchestrator + Decision Engine.

Turns LT Metrics's linear pipeline into an adaptive loop. The Orchestrator coordinates
the build-time agents (Generator -> Reviewer -> Repair -> Validation) and, on each
iteration, asks the Decision Engine what to do next:

    CONTINUE       everything passed — proceed to execution
    RETRY          a KNOWN failure — apply a deterministic KB repair and re-review
    INVOKE_CLAUDE  an UNKNOWN failure — escalate to the LLM for a fix
    STOP           a terminal condition (e.g. product out of stock) — halt cleanly
    ESCALATE       retries exhausted — hand back to a human with full context

The Decision Engine is a pure rules engine (no LLM): it consults the Knowledge
Base and target Memory to route platform behaviour and classify failures. Claude
is only ever reached when the KB has NO rule for a failure — so accumulated
knowledge steadily reduces LLM usage over time.

Design: the agents are injected as callables (`PipelineSteps`) so this module
has no hard import cycle with the concrete agents and is easy to unit-test. The
existing server/cli pipeline keeps working unchanged; it can opt into the
Orchestrator by handing it those callables.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import db, memory
from ..knowledge import KB

# ---- decisions ------------------------------------------------------------ #
CONTINUE = "CONTINUE"
RETRY = "RETRY"
INVOKE_CLAUDE = "INVOKE_CLAUDE"
STOP = "STOP"
ESCALATE = "ESCALATE"

# Failures that are terminal for a run — no repair can help; stop cleanly.
_TERMINAL_BUGS = {"product_out_of_stock"}


@dataclass
class Decision:
    action: str
    reason: str
    repair_action: str = ""          # KB repair action id when RETRY
    bug_id: str = ""                 # matched known-bug id, if any


class DecisionEngine:
    """Deterministic control-flow + platform routing. No LLM."""

    def __init__(self, claude_available: bool = False) -> None:
        self.claude_available = claude_available

    # -- platform routing (the "is Magento -> REST checkout" style rules) -- #
    def route_checkout(self, platform: str, has_rest: bool) -> str:
        """REST checkout when the platform declares a REST checkout adapter (in the
        KB) AND REST was detected in the recording; else faithful recorded replay.
        KB-driven, so onboarding a platform doesn't touch this code."""
        mode = str(KB.platform_rules(platform).get("checkout", "")).lower()
        if mode == "rest" and has_rest:
            return "rest-checkout"
        return "recorded-replay"

    def payment_policy(self, platform: str, available_methods: list[str]) -> dict:
        """Never replay a hosted-gateway card token; prefer an offline method."""
        offline = KB.magento("offline_payments") or []
        hosted = KB.magento("hosted_gateways") or []
        pick = next((c for pref in offline for c in available_methods
                     if pref in (c or "").lower()), None)
        if not pick:
            pick = next((c for c in available_methods
                         if not any(g in (c or "").lower() for g in hosted)), None)
        return {"method": pick, "replay_token": False,
                "stop_if_none": True, "available": available_methods}

    # -- the core control-flow decision ------------------------------------ #
    def decide(self, error_text: str, attempt: int, max_attempts: int) -> Decision:
        if not error_text:
            return Decision(CONTINUE, "no failure")

        bug = KB.classify_error(error_text)
        if bug:
            if bug.get("id") in _TERMINAL_BUGS:
                return Decision(STOP, "terminal: %s" % bug["id"], bug_id=bug["id"])
            repair = KB.repair_for(error_text)
            if repair and attempt < max_attempts:
                return Decision(RETRY, "known bug %s" % bug["id"],
                                repair_action=repair.get("action", ""),
                                bug_id=bug["id"])
            if attempt >= max_attempts:
                return Decision(ESCALATE, "known bug %s but retries exhausted" % bug["id"],
                                bug_id=bug["id"])

        # Unknown failure: escalate to Claude if we can, else hand off.
        if self.claude_available and attempt < max_attempts:
            return Decision(INVOKE_CLAUDE, "unknown failure — ask Claude")
        return Decision(ESCALATE, "unknown failure and no LLM available"
                        if not self.claude_available else "retries exhausted")


@dataclass
class PipelineSteps:
    """Injected agent callables so the Orchestrator stays decoupled.

    generate(facts) -> script_path/obj
    review(script)  -> (ok: bool, findings: list)
    validate(script)-> (ok: bool, issues: list)     # optional Validation layer
    repair(script, error_text, use_claude) -> script (updated)
    error_of(review_findings, validate_issues) -> str   # extract an error signature
    """
    generate: Callable
    review: Callable
    repair: Callable
    validate: Optional[Callable] = None
    error_of: Optional[Callable] = None


@dataclass
class BuildResult:
    status: str                      # PASS | STOPPED | ESCALATED
    script: object = None
    attempts: int = 0
    trail: list = field(default_factory=list)   # decisions taken, for the UI/log


class Orchestrator:
    """Coordinates the build-time agent loop with the Decision Engine."""

    def __init__(self, target_url: str = "", claude_available: bool = False,
                 max_attempts: int = 3) -> None:
        self.target_url = target_url
        self.max_attempts = max_attempts
        self.engine = DecisionEngine(claude_available=claude_available)
        self.facts = memory.facts_for(target_url) if target_url else {}

    def build_and_validate(self, steps: PipelineSteps) -> BuildResult:
        """Generator -> Reviewer -> (Validation) -> Repair -> ... until PASS,
        STOP or ESCALATE. Applies target Memory + KB up front so prior runs'
        lessons are baked in before the first attempt."""
        trail: list[dict] = []
        script = steps.generate(self.facts)          # facts = KB + learned memory
        attempt = 0
        while True:
            attempt += 1
            r_ok, findings = steps.review(script)
            v_ok, issues = (steps.validate(script) if steps.validate else (True, []))
            if r_ok and v_ok:
                trail.append({"attempt": attempt, "decision": CONTINUE})
                return BuildResult("PASS", script, attempt, trail)

            err = ""
            if steps.error_of:
                err = steps.error_of(findings, issues) or ""
            elif findings or issues:
                err = " ".join(str(x) for x in (list(findings) + list(issues)))

            d = self.engine.decide(err, attempt, self.max_attempts)
            trail.append({"attempt": attempt, "decision": d.action,
                          "reason": d.reason, "bug": d.bug_id,
                          "repair": d.repair_action})

            if d.action in (STOP,):
                return BuildResult("STOPPED", script, attempt, trail)
            if d.action in (ESCALATE,):
                return BuildResult("ESCALATED", script, attempt, trail)
            # RETRY (deterministic) or INVOKE_CLAUDE (LLM) both go through repair.
            script = steps.repair(script, err, use_claude=(d.action == INVOKE_CLAUDE))

    # -- Store/Learn step (called after a run finishes) -------------------- #
    def learn(self, flow: dict, run_id: str = "", project_id: Optional[int] = None) -> list:
        """Persist what this run taught, so the next run's `facts` improve."""
        try:
            return memory.learn_from_flow(self.target_url, flow, run_id=run_id,
                                          project_id=project_id)
        except Exception:
            return []
