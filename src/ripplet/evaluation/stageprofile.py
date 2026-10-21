"""Opt-in stage-level profiler for the indexing pipeline (Phase 15).

Measures the REAL stages of ``service.index_workspace`` with a monotonic
high-resolution clock. Disabled by default: normal CLI/MCP output is
byte-identical when profiling is off (no ``stage_profile`` key is added).
Benchmark tooling enables it via ``index_workspace(cfg, profile=True)`` and
reads ``summary["stage_profile"]`` — machine-readable JSON with stage
durations, work counters, and environment notes.

Overhead when enabled: one ``perf_counter`` pair per stage plus counter
stores (no per-node logging in hot loops). Overhead when disabled: a single
``None`` check per stage boundary.
"""
from __future__ import annotations
import time


class StageProfiler:
    def __init__(self):
        self._t0 = time.perf_counter()
        self.stages: dict[str, float] = {}
        self.counters: dict[str, int] = {}
        self._open: dict[str, float] = {}

    def begin(self, name: str) -> None:
        self._open[name] = time.perf_counter()

    def end(self, name: str) -> None:
        started = self._open.pop(name, None)
        if started is None:
            return
        self.stages[name] = round(self.stages.get(name, 0.0) +
                                  (time.perf_counter() - started), 4)

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def set(self, name: str, n: int) -> None:
        self.counters[name] = n

    # Sub-stages nest inside their parent (their time is part of the
    # parent's); top-level sums exclude them so totals reconcile.
    NESTED = {"name_resolve": {"name_gather", "name_match", "name_attribute"},
              "py_resolve": {"py_collect", "py_pass1", "py_pass2"},
              "js_resolve": {"js_collect", "js_bind"}}

    def report(self) -> dict:
        total = round(time.perf_counter() - self._t0, 4)
        children = set().union(*self.NESTED.values()) if self.NESTED else set()
        top = {k: v for k, v in self.stages.items() if k not in children}
        accounted = round(sum(top.values()), 4)
        return {"stages_s": dict(self.stages),
                "top_level_s": top,
                "counters": dict(self.counters),
                "total_s": total,
                "unaccounted_s": round(total - accounted, 4)}


class _NullProfiler:
    """Zero-cost stand-in: every method is a no-op."""

    def begin(self, name: str) -> None:
        pass

    def end(self, name: str) -> None:
        pass

    def count(self, name: str, n: int = 1) -> None:
        pass

    def set(self, name: str, n: int) -> None:
        pass

    def report(self) -> dict:
        return {}


def make_profiler(enabled: bool):
    return StageProfiler() if enabled else _NullProfiler()
