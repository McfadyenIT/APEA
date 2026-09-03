"""Execution engine interface.

An engine turns a plan into the command(s) that actually generate load. Locust is
the default and only fully-implemented engine today; k6 / JMeter are registered
stubs so the rest of LT Metrics (Orchestrator, Executor) can stay engine-agnostic and a
new engine is an adapter, not a core change.
"""
from __future__ import annotations


class ExecutionEngine:
    name = "base"

    def available(self) -> bool:
        """True if this engine's runtime is installed on the machine."""
        raise NotImplementedError

    def supports_distributed(self) -> bool:
        return False

    def single_cmd(self, plan_cfg: dict, target_url: str) -> list[str]:
        """Command to run one load process, headless, from the run dir."""
        raise NotImplementedError

    def master_cmd(self, plan_cfg: dict, target_url: str, workers: int) -> list[str]:
        raise NotImplementedError

    def worker_cmd(self) -> list[str]:
        raise NotImplementedError
