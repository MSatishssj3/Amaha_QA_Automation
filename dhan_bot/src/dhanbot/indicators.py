"""Incremental (O(1) per update) indicators - no pandas, safe to use on the hot path.

Every indicator exposes ``update(...) -> value | None``, ``.value`` and ``.ready``.
"""

from __future__ import annotations

from collections import deque


class EMA:
    """Exponential moving average, seeded with the SMA of the first ``period`` values."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
                self._seed = []
            return self.value
        self.value += self.alpha * (x - self.value)
        return self.value


class SMA:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._q: deque[float] = deque()
        self._sum = 0.0
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        self._q.append(x)
        self._sum += x
        if len(self._q) > self.period:
            self._sum -= self._q.popleft()
        if len(self._q) == self.period:
            self.value = self._sum / self.period
        return self.value


class RSI:
    """Wilder's RSI."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        if self._prev is None:
            self._prev = x
            return None
        ch = x - self._prev
        self._prev = x
        gain, loss = max(ch, 0.0), max(-ch, 0.0)
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            n = self.period
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n  # type: ignore[operator]
        if self._avg_loss == 0:
            self.value = 100.0 if self._avg_gain > 0 else 50.0
        else:
            rs = self._avg_gain / self._avg_loss  # type: ignore[operator]
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    """Wilder's average true range. ``update(high, low, close)``."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev_close: float | None = None
        self._trs: list[float] = []
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, high: float, low: float, close: float) -> float | None:
        pc = self._prev_close
        tr = high - low if pc is None else max(high - low, abs(high - pc), abs(low - pc))
        self._prev_close = close
        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) == self.period:
                self.value = sum(self._trs) / self.period
                self._trs = []
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class VWAP:
    """Session VWAP. ``update(price, volume)``; call ``reset()`` at the session start."""

    def __init__(self) -> None:
        self._pv = 0.0
        self._v = 0.0
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, price: float, volume: float) -> float | None:
        if volume > 0:
            self._pv += price * volume
            self._v += volume
            self.value = self._pv / self._v
        return self.value

    def reset(self) -> None:
        self._pv = self._v = 0.0
        self.value = None


class _Rolling:
    def __init__(self, period: int, is_max: bool) -> None:
        self.period = period
        self._is_max = is_max
        self._q: deque[tuple[int, float]] = deque()
        self._i = 0
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self._i >= self.period

    def update(self, x: float) -> float | None:
        q = self._q
        if self._is_max:
            while q and q[-1][1] <= x:
                q.pop()
        else:
            while q and q[-1][1] >= x:
                q.pop()
        q.append((self._i, x))
        if q[0][0] <= self._i - self.period:
            q.popleft()
        self._i += 1
        self.value = q[0][1]
        return self.value


class RollingMax(_Rolling):
    def __init__(self, period: int) -> None:
        super().__init__(period, True)


class RollingMin(_Rolling):
    def __init__(self, period: int) -> None:
        super().__init__(period, False)
