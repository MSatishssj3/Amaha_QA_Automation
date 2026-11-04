"""Session timeline (IST) and timers. Works with every clock (real / sim / virtual)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Callable

from .clock import RealClock, epoch_at, ist_datetime, is_trading_day
from .config import AppConfig

log = logging.getLogger(__name__)

EVENTS = ("preflight", "feed_connect", "market_open", "entry_start", "entry_end", "squareoff_time", "session_end")


class SessionTimes:
    def __init__(self, cfg: AppConfig, holidays: set[date] | None = None) -> None:
        self.cfg = cfg
        self.holidays = holidays or set()
        self._cache: dict[date, dict[str, float]] = {}

    def for_day(self, day: date) -> dict[str, float]:
        t = self._cache.get(day)
        if t is None:
            times = self.cfg.session_times()
            t = self._cache[day] = {k: epoch_at(day, times[k]) for k in EVENTS}
        return t

    def today(self, now: float) -> dict[str, float]:
        return self.for_day(ist_datetime(now).date())

    def is_trading_day(self, now: float) -> bool:
        return is_trading_day(ist_datetime(now).date(), self.holidays)

    def anchor(self, epoch: float) -> float:
        """Bar anchor: the session open of the IST day containing ``epoch``."""
        return self.today(epoch)["market_open"]

    def in_entry_window(self, now: float) -> bool:
        t = self.today(now)
        return self.is_trading_day(now) and t["entry_start"] <= now < t["entry_end"]

    def market_hours(self, now: float) -> bool:
        t = self.today(now)
        return self.is_trading_day(now) and t["market_open"] - 900 <= now < t["session_end"]


class Scheduler:
    def __init__(self, clock: RealClock) -> None:
        self.clock = clock
        self._handles: list = []

    def at(self, epoch: float, name: str, cb: Callable[[], None], run_if_past: bool = False) -> bool:
        delay = epoch - self.clock.time()
        if delay < 0 and not run_if_past:
            return False

        def fire() -> None:
            log.info("scheduler: %s", name)
            try:
                cb()
            except Exception:
                log.exception("scheduled job %s failed", name)

        self._handles.append(self.clock.call_later(max(0.0, delay), fire))
        return True

    def cancel_all(self) -> None:
        for h in self._handles:
            try:
                h.cancel()
            except Exception:
                pass
        self._handles.clear()
