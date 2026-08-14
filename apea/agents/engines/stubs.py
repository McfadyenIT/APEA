"""Registerable stubs for other engines. They advertise availability (binary
present) but raise a clear NotImplementedError until an adapter is written — so
the architecture supports them without pretending they work."""
from __future__ import annotations

import shutil

from .base import ExecutionEngine


class _NotYet(ExecutionEngine):
    _bin = ""

    def available(self) -> bool:
        return bool(shutil.which(self._bin))

    def single_cmd(self, plan_cfg, target_url):
        raise NotImplementedError("%s engine adapter is not implemented yet" % self.name)

    master_cmd = single_cmd

    def worker_cmd(self):
        raise NotImplementedError("%s engine adapter is not implemented yet" % self.name)


class K6Engine(_NotYet):
    name = "k6"
    _bin = "k6"


class JMeterEngine(_NotYet):
    name = "jmeter"
    _bin = "jmeter"
