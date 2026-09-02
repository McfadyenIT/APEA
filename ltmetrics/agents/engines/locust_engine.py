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
        """Is Locust installed?

        Answered WITHOUT importing it. Importing locust runs gevent's
        monkey.patch_all(), and off the main thread that hangs the process --
        a fresh server died on the first /api/health, which the page calls on
        load. find_spec locates the module without executing it.
        """
        if shutil.which("locust"):
            return True
        try:
            import importlib.util
            return importlib.util.find_spec("locust") is not None
        except Exception:
            return False

    def supports_distributed(self) -> bool:
        return True

    def _launcher(self) -> list[str]:
        b = shutil.which("locust")
        return [b] if b else [sys.executable, "-m", "locust"]

    def _common_args(self, plan_cfg: dict, target_url: str) -> list[str]:
        args = [
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
        # STEADY STATE. Without this, the statistics cover the whole run from the
        # first spawned user, so a quoted p95 blends the ramp with the steady
        # state. On a short smoke test that is noise; on a long ramp the early,
        # uncontended samples dominate and the reported percentile is not the
        # percentile of anything anyone cares about. --reset-stats discards
        # everything measured before the last user spawned; the requests are
        # still sent, they just stop skewing the numbers.
        #
        # On by default. Set measure_ramp_up: true in the plan to keep the ramp
        # in the figures -- occasionally wanted when the ramp itself is the
        # thing under test.
        if not plan_cfg.get("measure_ramp_up"):
            args.append("--reset-stats")
        return args

    def single_cmd(self, plan_cfg: dict, target_url: str) -> list[str]:
        return [*self._launcher(), *self._common_args(plan_cfg, target_url)]

    def master_cmd(self, plan_cfg: dict, target_url: str, workers: int) -> list[str]:
        return [*self._launcher(), *self._common_args(plan_cfg, target_url),
                "--master", "--expect-workers", str(workers)]

    def worker_cmd(self) -> list[str]:
        return [*self._launcher(), "-f", "scripts/locustfile.py",
                "--worker", "--master-host", "127.0.0.1"]
