"""Tick -> OHLCV candle aggregation, aligned to the IST session open.

* Buckets are anchored at the session's market open (09:15 IST for NSE), so 5-minute bars close at
  09:20, 09:25, ... exactly like exchange charts.
* Volume comes from the cumulative day volume in quote / full packets (ticker packets carry none).
* A bar is closed on the first tick of the next bucket, or by ``flush(now)`` once ``grace_s`` has
  passed after the bucket end - so bars close on time even in illiquid instruments.
* Completed bars live in fixed-size ring buffers: ``series.close[-1]`` is the last closed bar.
"""

from __future__ import annotations

import math
import re
from collections import deque
from typing import Any, Callable

from .models import Bar

_TF = re.compile(r"^(\d+)\s*([smhd])$", re.I)
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_timeframe(tf: str | int) -> int:
    if isinstance(tf, int):
        return tf
    m = _TF.match(tf.strip())
    if not m:
        raise ValueError(f"bad timeframe {tf!r}: use e.g. 5s, 1m, 15m, 1h, 1d")
    secs = int(m.group(1)) * _UNIT[m.group(2).lower()]
    if secs <= 0:
        raise ValueError("timeframe must be positive")
    return secs


class BarSeries:
    def __init__(self, key: tuple[int, int], symbol: str, tf: int, maxlen: int = 1000) -> None:
        self.key = key
        self.symbol = symbol
        self.tf = tf
        self.start: deque[float] = deque(maxlen=maxlen)
        self.open: deque[float] = deque(maxlen=maxlen)
        self.high: deque[float] = deque(maxlen=maxlen)
        self.low: deque[float] = deque(maxlen=maxlen)
        self.close: deque[float] = deque(maxlen=maxlen)
        self.volume: deque[int] = deque(maxlen=maxlen)
        self.cur: Bar | None = None
        self._indicators: list[tuple[Any, str]] = []

    def __len__(self) -> int:
        return len(self.close)

    def last(self) -> Bar | None:
        if not self.close:
            return None
        return Bar(self.key, self.symbol, self.tf, self.start[-1], self.open[-1], self.high[-1], self.low[-1],
                   self.close[-1], self.volume[-1])

    def attach(self, indicator: Any, field: str = "close") -> Any:
        """Feed ``indicator`` every closed bar (and all bars already in the buffer)."""
        if field not in ("close", "open", "high", "low", "hl2", "hlc3", "hlc", "pv"):
            raise ValueError("field must be close|open|high|low|hl2|hlc3|hlc|pv")
        self._indicators.append((indicator, field))
        for i in range(len(self.close)):
            self._feed(indicator, field, self.open[i], self.high[i], self.low[i], self.close[i], self.volume[i])
        return indicator

    @staticmethod
    def _feed(ind: Any, field: str, o: float, h: float, lo: float, c: float, v: int) -> None:
        if field == "close":
            ind.update(c)
        elif field == "hlc":
            ind.update(h, lo, c)
        elif field == "pv":
            ind.update((h + lo + c) / 3, v)
        elif field == "hl2":
            ind.update((h + lo) / 2)
        elif field == "hlc3":
            ind.update((h + lo + c) / 3)
        else:
            ind.update({"open": o, "high": h, "low": lo}[field])

    def _push(self, b: Bar) -> None:
        self.start.append(b.start)
        self.open.append(b.open)
        self.high.append(b.high)
        self.low.append(b.low)
        self.close.append(b.close)
        self.volume.append(b.volume)
        for ind, field in self._indicators:
            self._feed(ind, field, b.open, b.high, b.low, b.close, b.volume)

    def load_history(self, bars: list[Bar]) -> None:
        for b in sorted(bars, key=lambda x: x.start):
            if self.start and b.start <= self.start[-1]:
                continue
            self._push(b)


class BarAggregator:
    def __init__(self, anchor_fn: Callable[[float], float], grace_s: float = 1.0) -> None:
        self.anchor_fn = anchor_fn  # epoch -> session-open epoch of that day
        self.grace_s = grace_s
        self.series: dict[tuple[int, int], list[BarSeries]] = {}
        self._cum_vol: dict[tuple[int, int], int] = {}

    def add(self, key: tuple[int, int], symbol: str, tf: int | str, maxlen: int = 1000) -> BarSeries:
        tf_s = parse_timeframe(tf)
        for s in self.series.get(key, []):
            if s.tf == tf_s:
                return s
        s = BarSeries(key, symbol, tf_s, maxlen)
        self.series.setdefault(key, []).append(s)
        return s

    def get(self, key: tuple[int, int], tf: int | str) -> BarSeries | None:
        tf_s = parse_timeframe(tf)
        for s in self.series.get(key, []):
            if s.tf == tf_s:
                return s
        return None

    def bucket_start(self, t: float, tf: int) -> float:
        anchor = self.anchor_fn(t)
        if tf >= 86400:
            return anchor
        return anchor + math.floor((t - anchor) / tf) * tf

    def on_tick(self, key: tuple[int, int], t: float, price: float, cum_volume: int | None) -> list[Bar]:
        lst = self.series.get(key)
        if not lst:
            return []
        vol = 0
        if cum_volume is not None:
            prev = self._cum_vol.get(key)
            if prev is not None and cum_volume >= prev:
                vol = cum_volume - prev
            self._cum_vol[key] = cum_volume
        closed: list[Bar] = []
        for s in lst:
            start = self.bucket_start(t, s.tf)
            cur = s.cur
            if cur is not None and start > cur.start:
                s._push(cur)
                closed.append(cur)
                cur = None
            if cur is None:
                if s.start and start <= s.start[-1]:  # late tick for an already-closed bar
                    continue
                s.cur = Bar(key, s.symbol, s.tf, start, price, price, price, price, vol)
            else:
                if price > cur.high:
                    cur.high = price
                if price < cur.low:
                    cur.low = price
                cur.close = price
                cur.volume += vol
        return closed

    def flush(self, now: float) -> list[Bar]:
        closed: list[Bar] = []
        for lst in self.series.values():
            for s in lst:
                cur = s.cur
                if cur is not None and now >= cur.start + s.tf + self.grace_s:
                    s._push(cur)
                    s.cur = None
                    closed.append(cur)
        return closed
