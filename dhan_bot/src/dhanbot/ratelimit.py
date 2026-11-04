"""Client-side rate limiting.

* ``OrderRateLimiter`` - non-blocking gate for order APIs (place / modify / cancel all count):
  strict per-second and per-minute sliding windows plus a daily cap. The OMS asks ``try_acquire()``; exits
  that cannot go immediately are queued with priority, entries are rejected (or wait briefly).
* ``AsyncLimiter`` - awaitable bucket for non-order REST calls (non-trading / data / quote).

Ceilings (10/s, 250/min) are enforced in ``config.validate`` - config can only go lower.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable


class SlidingWindow:
    """At most ``limit`` events in any rolling ``window`` seconds (strict - unlike a token bucket,
    which can let ``2 x limit`` through around a window boundary)."""

    __slots__ = ("limit", "window", "_events", "_now")

    def __init__(self, limit: int, window: float, now: Callable[[], float] = time.monotonic) -> None:
        if limit <= 0 or window <= 0:
            raise ValueError("limit and window must be > 0")
        self.limit = int(limit)
        self.window = float(window)
        self._events: deque[float] = deque()
        self._now = now

    def _trim(self, now: float) -> None:
        ev = self._events
        edge = now - self.window
        while ev and ev[0] <= edge:
            ev.popleft()

    def available(self) -> int:
        self._trim(self._now())
        return self.limit - len(self._events)

    def try_take(self) -> bool:
        now = self._now()
        self._trim(now)
        if len(self._events) < self.limit:
            self._events.append(now)
            return True
        return False

    def take_unchecked(self, now: float) -> None:
        self._events.append(now)

    def wait_time(self) -> float:
        now = self._now()
        self._trim(now)
        if len(self._events) < self.limit:
            return 0.0
        return max(0.0, self._events[0] + self.window - now)

    def fill(self) -> None:
        """Pretend the window is full right now (used for broker-signalled cooldowns)."""
        now = self._now()
        self._trim(now)
        while len(self._events) < self.limit:
            self._events.append(now)


class OrderRateLimiter:
    def __init__(self, per_sec: int, per_min: int, per_day: int, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self.sec = SlidingWindow(per_sec, 1.0, now)
        self.min = SlidingWindow(per_min, 60.0, now)
        self.per_day = per_day
        self.used_today = 0
        self._blocked_until = 0.0

    def try_acquire(self) -> bool:
        now = self._now()
        if now < self._blocked_until or self.used_today >= self.per_day:
            return False
        if self.sec.available() >= 1 and self.min.available() >= 1:
            self.sec.take_unchecked(now)
            self.min.take_unchecked(now)
            self.used_today += 1
            return True
        return False

    def wait_time(self) -> float:
        if self.used_today >= self.per_day:
            return float("inf")
        block = max(0.0, self._blocked_until - self._now())
        return max(block, self.sec.wait_time(), self.min.wait_time())

    def cooldown(self, seconds: float) -> None:
        """Broker said 'too many requests': stop sending for a while."""
        self._blocked_until = max(self._blocked_until, self._now() + seconds)
        self.sec.fill()

    def reset_day(self) -> None:
        self.used_today = 0

    @property
    def remaining_today(self) -> int:
        return max(0, self.per_day - self.used_today)


class AsyncLimiter:
    def __init__(self, per_sec: float, now: Callable[[], float] = time.monotonic) -> None:
        self.window = SlidingWindow(max(1, int(per_sec)), 1.0, now)
        self._blocked_until = 0.0
        self._now = now
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:  # FIFO fairness between waiters
            while True:
                block = self._blocked_until - self._now()
                if block > 0:
                    await asyncio.sleep(block)
                    continue
                if self.window.try_take():
                    return
                await asyncio.sleep(max(0.001, self.window.wait_time()))

    def cooldown(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._now() + seconds)
