"""Execution engine registry. Locust is the default; k6/JMeter are stubs.

    from .engines import get_engine
    eng = get_engine(plan_cfg.get("engine", "locust"))
    cmd = eng.single_cmd(plan_cfg, target_url)
"""
from .base import ExecutionEngine
from .locust_engine import LocustEngine
from .stubs import K6Engine, JMeterEngine
from .playwright_engine import PlaywrightEngine

_REGISTRY: dict[str, ExecutionEngine] = {}


def register(name: str, engine: ExecutionEngine) -> None:
    _REGISTRY[name.lower()] = engine


def get_engine(name: str = "locust") -> ExecutionEngine:
    """Return the requested engine, falling back to Locust."""
    return _REGISTRY.get((name or "locust").lower(), _REGISTRY["locust"])


def available_engines() -> dict[str, bool]:
    return {n: e.available() for n, e in _REGISTRY.items()}


register("locust", LocustEngine())
register("k6", K6Engine())
register("jmeter", JMeterEngine())
register("playwright", PlaywrightEngine())

__all__ = ["ExecutionEngine", "get_engine", "register", "available_engines"]
