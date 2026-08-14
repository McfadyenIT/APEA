"""Locust engine adapter — the default. Produces exactly the commands the
executor has always used, now behind the ExecutionEngine interface."""
from __future__ import annotations

import shutil
import sys

from .base import ExecutionEngine

RESULTS_PREFIX = "results/locust"


class LocustEngine(ExecutionEngine):
    name = "locust"

    def available(self) -> bool:
        if shutil.which("locust"):
            return True
        try:
            import locust  # noqa: F401
            return True
        except Exception:
            return False

    def supports_distributed(self) -> bool:
        return True

    def _launcher(self) -> list[str]:
        b = shutil.which("locust")
        return [b] if b else [sys.executable, "-m", "locust"]

    def _common_args(self, plan_cfg: dict, target_url: str) -> list[str]:
        return [
            "-f", "scripts/locustfile.py",
            "--host", target_url,
            "--headless",
            "-u", str(plan_cfg["users"]),
            "-r", str(plan_cfg["spawn_rate"]),
            "--run-time", "%ss" % plan_cfg["duration_s"],
            "--csv", RESULTS_PREFIX,
            "--csv-full-history",
            "--html", "results/locust_raw.html",
            "--only-summary",
            "--loglevel", "INFO",
        ]

    def single_cmd(self, plan_cfg: dict, target_url: str) -> list[str]:
        return [*self._launcher(), *self._common_args(plan_cfg, target_url)]

    def master_cmd(self, plan_cfg: dict, target_url: str, workers: int) -> list[str]:
        return [*self._launcher(), *self._common_args(plan_cfg, target_url),
                "--master", "--expect-workers", str(workers)]

    def worker_cmd(self) -> list[str]:
        return [*self._launcher(), "-f", "scripts/locustfile.py",
                "--worker", "--master-host", "127.0.0.1"]
