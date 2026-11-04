"""Time handling: IST helpers, trading calendar, and pluggable clocks.

Three clocks share one interface so the *same engine code* runs live, in accelerated
simulation and in deterministic replay:

* ``RealClock``    - wall time, timers on the asyncio loop.
* ``SimClock``     - wall time shifted to a chosen start and sped up N times (``dhanbot simulate``).
* ``VirtualClock`` - time only moves when ``advance_to()`` is called (``dhanbot replay`` / tests).

All business logic asks the clock for ``time()`` (UTC epoch seconds). Latency measurements use
``time.perf_counter_ns()`` directly and never go through a clock.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
IST_OFFSET_S = 19800  # +05:30


def parse_hhmm(text: str) -> tuple[int, int, int]:
    parts = [int(p) for p in text.strip().split(":")]
    if len(parts) == 2:
        parts.append(0)
    if len(parts) != 3 or not (0 <= parts[0] < 24 and 0 <= parts[1] < 60 and 0 <= parts[2] < 60):
        raise ValueError(f"invalid time {text!r}, expected HH:MM or HH:MM:SS")
    return parts[0], parts[1], parts[2]


def ist_datetime(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, IST)


def epoch_at(day: date, hhmm: str) -> float:
    """UTC epoch for an IST wall-clock time on a given IST date."""
    h, m, s = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST).timestamp()


class _Handle:
    __slots__ = ("cancelled", "when", "cb", "args")

    def __init__(self, when: float, cb: Callable, args: tuple) -> None:
        self.when = when
        self.cb = cb
        self.args = args
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class RealClock:
    speed = 1.0
    virtual = False

    def time(self) -> float:
        return time.time()

    def now(self) -> datetime:
        return ist_datetime(self.time())

    def today(self) -> date:
        return self.now().date()

    def call_later(self, delay: float, cb: Callable, *args):
        return asyncio.get_running_loop().call_later(max(0.0, delay) / self.speed, cb, *args)

    def call_at(self, epoch: float, cb: Callable, *args):
        return self.call_later(epoch - self.time(), cb, *args)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) / self.speed)


class SimClock(RealClock):
    """Wall clock that starts at ``start_epoch`` and runs ``speed`` times faster than real time."""

    def __init__(self, start_epoch: float, speed: float = 1.0) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.speed = float(speed)
        self._start = start_epoch
        self._p0 = time.perf_counter()

    def time(self) -> float:
        return self._start + (time.perf_counter() - self._p0) * self.speed


class VirtualClock(RealClock):
    """Deterministic clock for replay/tests: time advances only via ``advance_to``."""

    virtual = True

    def __init__(self, start_epoch: float) -> None:
        self._t = float(start_epoch)
        self._heap: list[tuple[float, int, _Handle]] = []
        self._seq = itertools.count()

    def time(self) -> float:
        return self._t

    def call_later(self, delay: float, cb: Callable, *args):
        h = _Handle(self._t + max(0.0, delay), cb, args)
        heapq.heappush(self._heap, (h.when, next(self._seq), h))
        return h

    def call_at(self, epoch: float, cb: Callable, *args):
        return self.call_later(epoch - self._t, cb, *args)

    async def sleep(self, seconds: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self.call_later(seconds, lambda: fut.done() or fut.set_result(None))
        await fut

    def advance_to(self, epoch: float) -> None:
        """Move time forward, firing due timers in order (timers may schedule more timers)."""
        while self._heap and self._heap[0][0] <= epoch:
            when, _, h = heapq.heappop(self._heap)
            if when > self._t:
                self._t = when
            if not h.cancelled:
                try:
                    h.cb(*h.args)
                except Exception:  # a timer must never break the replay loop
                    log.exception("virtual timer callback failed")
        if epoch > self._t:
            self._t = epoch

    def advance(self, seconds: float) -> None:
        self.advance_to(self._t + seconds)

    def pending(self) -> int:
        return sum(1 for _, _, h in self._heap if not h.cancelled)


# ---------------------------------------------------------------------------------------------
# Trading calendar
# ---------------------------------------------------------------------------------------------


def load_holidays(path: Path | None) -> set[date]:
    """Read ``YYYY-MM-DD`` lines (``#`` comments allowed). Missing file -> empty set."""
    out: set[date] = set()
    if path is None or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            out.add(date.fromisoformat(line[:10]))
        except ValueError:
            log.warning("ignoring bad holiday line %r in %s", line, path)
    return out


def is_trading_day(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def next_trading_day(day: date, holidays: set[date]) -> date:
    d = day + timedelta(days=1)
    while not is_trading_day(d, holidays):
        d += timedelta(days=1)
    return d


# ---------------------------------------------------------------------------------------------
# LTT (last-trade-time) normalisation
# ---------------------------------------------------------------------------------------------


class LttNormalizer:
    """Converts the feed's LTT field to a true UTC epoch.

    Dhan's feed appears to encode IST wall-clock time as if it were UTC (the official SDK formats
    LTT with a UTC formatter and gets IST wall time). We do not rely on that: in ``auto`` mode the
    first plausible sample is compared with the local clock. A difference of ~+5:30 means
    "IST-shifted", ~0 means "true UTC". Ticks with stale LTTs (pre-open, illiquid) are skipped.
    """

    def __init__(self, mode: str, clock: RealClock) -> None:
        if mode not in ("auto", "utc", "ist_shifted"):
            raise ValueError(f"bad ltt_mode {mode!r}")
        self.mode = mode
        self.clock = clock
        self.offset = IST_OFFSET_S if mode == "ist_shifted" else 0
        self.detected = mode != "auto"
        self._samples = 0

    def observe(self, raw_ltt: int) -> None:
        if self.detected or raw_ltt <= 0:
            return
        self._samples += 1
        diff = raw_ltt - self.clock.time()
        if abs(diff - IST_OFFSET_S) < 1800:
            self.offset, self.detected = IST_OFFSET_S, True
            log.info("LTT mode auto-detected: IST-shifted epoch (offset %ss)", IST_OFFSET_S)
        elif abs(diff) < 1800:
            self.offset, self.detected = 0, True
            log.info("LTT mode auto-detected: true UTC epoch")
        elif self._samples >= 500:
            self.detected = True
            log.warning("LTT mode could not be detected (last diff %.0fs); assuming UTC", diff)

    def to_epoch(self, raw_ltt: int) -> float:
        return float(raw_ltt - self.offset) if raw_ltt else 0.0
