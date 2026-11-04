"""Paths, clocks, the IST market calendar and logging - the runtime context every bot shares."""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from dhanbot.clock import IST, RealClock, epoch_at, ist_datetime, is_trading_day, load_holidays
from dhanbot.logging_setup import JsonFormatter, RedactingFormatter, register_secrets

from .bus import Bus
from .settings import Settings


class SharedSimClock(RealClock):
    """Accelerated clock that agrees across processes: ``start + (wall_now - wall_ref) * speed``.

    Every process of a simulated session gets the same ``start``/``wall_ref``/``speed`` (the
    supervisor passes them on the command line), so all bots see the same simulated time.
    """

    def __init__(self, start_epoch: float, speed: float, wall_ref: float | None = None) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.speed = float(speed)
        self.start = float(start_epoch)
        self.wall_ref = float(wall_ref if wall_ref is not None else time.time())

    def time(self) -> float:
        return self.start + (time.time() - self.wall_ref) * self.speed


class Paths:
    def __init__(self, base_dir: Path, settings: Settings) -> None:
        self.base = Path(base_dir).resolve()
        d = Path(settings.system.data_dir)
        self.data = d if d.is_absolute() else self.base / d
        lg = Path(settings.system.log_dir)
        self.logs = lg if lg.is_absolute() else self.base / lg
        self.tables = self.data / "tables"
        self.settings_file = self.base / "config" / "settings.toml"
        self.holidays_file = self.base / "config" / "holidays.txt"

    def bot(self, name: str) -> Path:
        p = self.data / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.base / p


class MarketCalendar:
    """NSE session calendar in IST (weekends + ``config/holidays.txt``)."""

    def __init__(self, holidays: set[date] | None = None, open_hhmm: str = "09:15", close_hhmm: str = "15:30") -> None:
        self.holidays = holidays or set()
        self.open_hhmm = open_hhmm
        self.close_hhmm = close_hhmm

    @classmethod
    def from_file(cls, path: Path) -> "MarketCalendar":
        return cls(load_holidays(path))

    def is_trading_day(self, d: date) -> bool:
        return is_trading_day(d, self.holidays)

    def session(self, d: date) -> tuple[float, float]:
        return epoch_at(d, self.open_hhmm), epoch_at(d, self.close_hhmm)

    def in_session(self, epoch: float, pre_s: float = 900, post_s: float = 600) -> bool:
        d = ist_datetime(epoch).date()
        if not self.is_trading_day(d):
            return False
        o, c = self.session(d)
        return o - pre_s <= epoch < c + post_s

    def next_time(self, epoch: float, hhmm: str, trading_days_only: bool = True) -> float:
        d = ist_datetime(epoch).date()
        for _ in range(15):
            t = epoch_at(d, hhmm)
            if t > epoch and (not trading_days_only or self.is_trading_day(d)):
                return t
            d += timedelta(days=1)
        return epoch_at(d, hhmm)

    def last_trading_day(self, d: date) -> date:
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d


@dataclass
class BotContext:
    bus: Bus
    paths: Paths
    clock: RealClock
    bootstrap: Settings  # settings as read from the file at process start (system section is authoritative)
    calendar: MarketCalendar = field(default_factory=MarketCalendar)
    sim: dict | None = None  # simulated-market connection details, when platform = simulated

    def today(self) -> date:
        return ist_datetime(self.clock.time()).date()


_listener: logging.handlers.QueueListener | None = None
_qh: logging.Handler | None = None


class _Stderr(logging.StreamHandler):
    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property  # type: ignore[override]
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


def setup_logging(log_dir: Path | None, name: str, level: str = "INFO", console: bool = True,
                  secrets: list[str] | None = None) -> None:
    """Queue-backed logging (hot paths never block on I/O) with secrets redacted; one JSONL file per bot."""
    global _listener, _qh
    shutdown_logging()
    register_secrets(secrets or [])
    handlers: list[logging.Handler] = []
    if console:
        h = _Stderr()
        h.setFormatter(RedactingFormatter(f"%(asctime)s %(levelname)-7s [{name}] %(name)s: %(message)s"))
        handlers.append(h)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(log_dir / f"{name}.jsonl", when="midnight", backupCount=30,
                                                       encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        handlers.append(fh)
    q: queue.SimpleQueue = queue.SimpleQueue()
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    _qh = logging.handlers.QueueHandler(q)
    root.addHandler(_qh)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for noisy in ("aiohttp.access", "urllib3", "httpx", "httpcore", "anthropic._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    _listener.start()


def shutdown_logging() -> None:
    global _listener, _qh
    if _qh is not None:
        logging.getLogger().removeHandler(_qh)
        _qh = None
    if _listener is not None:
        _listener.stop()
        for h in _listener.handlers:
            try:
                h.close()
            except Exception:
                pass
        _listener = None


def ist_now_str(clock: RealClock) -> str:
    return ist_datetime(clock.time()).strftime("%Y-%m-%d %H:%M:%S")


__all__ = ["IST", "SharedSimClock", "Paths", "MarketCalendar", "BotContext", "setup_logging", "shutdown_logging"]
