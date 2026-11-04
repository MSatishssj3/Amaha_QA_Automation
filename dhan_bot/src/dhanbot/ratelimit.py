"""Client-side rate limiting.

* ``OrderRateLimiter`` - non-blocking gate for order APIs (place / modify / cancel all count):
  per-second and per-minute token buckets plus a daily cap. The OMS asks ``try_acquire()``; exits
  that cannot go immediately are queued with priority, entries are rejected (or wait briefly).
* ``AsyncLimiter`` - awaitable bucket for non-order REST calls (non-trading / data / quote).

Ceilings (10/s, 250/min) are enforced in ``config.validate`` - config can only go lower.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable


class TokenBucket:
    __slots__ = ("rate", "capacity", "tokens", "last", "_now")

    def __init__(self, rate_per_s: float, capacity: float, now: Callable[[], float] = time.monotonic) -> None:
        if rate_per_s <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be > 0")
        self.rate = float(rate_per_s)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self._now = now
        self.last = now()

    def _refill(self, now: float) -> None:
        if now > self.last:
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now

    def available(self) -> float:
        self._refill(self._now())
        return self.tokens

    def try_take(self, n: float = 1.0) -> bool:
        self._refill(self._now())
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def wait_time(self, n: float = 1.0) -> float:
        self._refill(self._now())
        return 0.0 if self.tokens >= n else (n - self.tokens) / self.rate

    def drain(self) -> None:
        self._refill(self._now())
        self.tokens = 0.0


class OrderRateLimiter:
    def __init__(self, per_sec: int, per_min: int, per_day: int, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self.sec = TokenBucket(per_sec, per_sec, now)
        self.min = TokenBucket(per_min / 60.0, per_min, now)
        self.per_day = per_day
        self.used_today = 0
        self._blocked_until = 0.0

    def try_acquire(self) -> bool:
        now = self._now()
        if now < self._blocked_until or self.used_today >= self.per_day:
            return False
        if self.sec.available() >= 1 and self.min.available() >= 1:
            self.sec.tokens -= 1
            self.min.tokens -= 1
            self.used_today += 1
            return True
        return False

    def wait_time(self) -> float:
        if self.used_today >= self.per_day:
            return float("inf")
        block = max(0.0, self._blocked_until - self._now())
        return max(block, self.sec.wait_time(), self.min.wait_time())

    def cooldown(self, seconds: float) -> None:
        """Broker said 'too many requests': stop sending for a while and empty the per-second bucket."""
        self._blocked_until = max(self._blocked_until, self._now() + seconds)
        self.sec.drain()

    def reset_day(self) -> None:
        self.used_today = 0

    @property
    def remaining_today(self) -> int:
        return max(0, self.per_day - self.used_today)


class AsyncLimiter:
    def __init__(self, per_sec: float, now: Callable[[], float] = time.monotonic) -> None:
        self.bucket = TokenBucket(per_sec, max(1.0, per_sec), now)
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
                if self.bucket.try_take():
                    return
                await asyncio.sleep(self.bucket.wait_time())

    def cooldown(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._now() + seconds)
