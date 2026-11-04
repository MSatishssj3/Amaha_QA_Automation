"""Latency measurement: per-stage samples in nanoseconds -> p50/p95/p99/max reports.

Stages recorded by the engine / OMS:
    frame_parse          WS frame received -> packets parsed
    strategy_on_tick     time spent inside the strategy's on_tick
    tick_to_send         WS frame received -> order request handed to the HTTP client
    order_send_to_ack    REST round trip for order placement (network + Dhan OMS)
    order_send_to_fill   order sent -> first fill reported (network + exchange)
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(slots=True)
class StageStats:
    stage: str
    n: int
    p50_us: float
    p95_us: float
    p99_us: float
    max_us: float

    def as_dict(self) -> dict:
        return {"stage": self.stage, "n": self.n, "p50_us": round(self.p50_us, 1), "p95_us": round(self.p95_us, 1),
                "p99_us": round(self.p99_us, 1), "max_us": round(self.max_us, 1)}


def _pct(sorted_vals: list[int], q: float) -> float:
    """Nearest-rank percentile, in microseconds."""
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))
    return sorted_vals[i] / 1000.0


class LatencyTracker:
    def __init__(self, max_samples: int = 200_000) -> None:
        self.max_samples = max_samples
        self._period: dict[str, list[int]] = {}
        self._day: dict[str, list[int]] = {}

    def record(self, stage: str, ns: int) -> None:
        lst = self._period.get(stage)
        if lst is None:
            lst = self._period[stage] = []
        if len(lst) < self.max_samples:
            lst.append(ns)

    @staticmethod
    def _stats(data: dict[str, list[int]]) -> list[StageStats]:
        out = []
        for stage, vals in sorted(data.items()):
            if not vals:
                continue
            s = sorted(vals)
            out.append(StageStats(stage, len(s), _pct(s, 0.5), _pct(s, 0.95), _pct(s, 0.99), s[-1] / 1000.0))
        return out

    def snapshot(self) -> list[StageStats]:
        merged = {k: self._day.get(k, []) + v for k, v in self._period.items()}
        for k, v in self._day.items():
            merged.setdefault(k, v)
        return self._stats(merged)

    def roll(self) -> list[StageStats]:
        """Return stats for the period since the last roll and fold samples into the day totals."""
        stats = self._stats(self._period)
        for k, v in self._period.items():
            day = self._day.setdefault(k, [])
            room = self.max_samples - len(day)
            if room > 0:
                day.extend(v[:room])
        self._period = {}
        return stats

    def day(self) -> list[StageStats]:
        self.roll()
        return self._stats(self._day)


def format_stats(stats: list[StageStats]) -> str:
    if not stats:
        return "no latency samples yet"
    rows = [f"{'stage':<20} {'n':>7} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9}  (microseconds)"]
    for s in stats:
        rows.append(f"{s.stage:<20} {s.n:>7} {s.p50_us:>9.1f} {s.p95_us:>9.1f} {s.p99_us:>9.1f} {s.max_us:>9.1f}")
    return "\n".join(rows)
