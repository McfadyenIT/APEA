"""Playwright browser-execution engine (LT Metrics "Track B").

A real-browser load engine for the parts of a journey that HTTP replay cannot
drive — payment iFrames, Shadow DOM fields, 3DS / OAuth. It implements the same
`ExecutionEngine` interface as the Locust engine, so the rest of LT Metrics stays
engine-agnostic; the Executor launches the generated `scripts/playwright_runner.py`.

INERT BY DEFAULT: `available()` returns False unless Playwright is importable, and
the Executor only ever touches this engine when a run explicitly opts in
(`browser_vus > 0`). With the flag off or Playwright absent, this file is never
exercised and existing Locust runs are completely unaffected.
"""
from __future__ import annotations

import sys

from .base import ExecutionEngine


class PlaywrightEngine(ExecutionEngine):
    name = "playwright"

    def available(self) -> bool:
        """True only if Playwright is installed. (Browser binaries are checked at
        runtime by the runner; this keeps the check cheap and import-safe.)"""
        try:
            import playwright  # noqa: F401
            return True
        except Exception:
            return False

    def supports_distributed(self) -> bool:
        return False

    def single_cmd(self, plan_cfg: dict, target_url: str) -> list[str]:
        """Command to run the generated browser runner from the run dir. The runner
        script is written per-run by `browser_runner_gen.generate_browser_runner`."""
        bt = (plan_cfg or {}).get("browser_track") or {}
        vus = int(bt.get("vus") or 1)
        duration = int(plan_cfg.get("duration_s") or 60)
        ramp = int(bt.get("ramp") or max(1, min(vus, 10)))
        return [sys.executable, "scripts/playwright_runner.py",
                "--host", str(target_url or ""),
                "--vus", str(max(1, vus)),
                "--duration", str(max(1, duration)),
                "--ramp", str(max(1, ramp))]

    # Track B is single-node by design (browser pool is the bottleneck); it never
    # participates in Locust-style master/worker distribution.
    def master_cmd(self, plan_cfg: dict, target_url: str, workers: int) -> list[str]:
        return self.single_cmd(plan_cfg, target_url)

    def worker_cmd(self) -> list[str]:
        return []
