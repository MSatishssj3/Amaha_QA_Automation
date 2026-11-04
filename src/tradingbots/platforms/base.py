"""The interface every trading-platform adapter implements.

All methods return rows already shaped like the tables in ``core/schemas.py`` (platform-specific field
names never leak past the adapter), so every other bot is platform-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Callable, Protocol

from dhanbot.models import Instrument

from ..core.settings import WatchItem


class PlatformError(Exception):
    pass


class TickLike(Protocol):
    seg: int
    sid: int
    ltp: float
    volume: int
    oi: int
    code: int
    bids: tuple
    asks: tuple


class Stream(ABC):
    """A live market-data connection."""

    @abstractmethod
    async def run(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def recycle(self) -> None:
        """Reconnect (e.g. after the access token changed)."""

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @property
    @abstractmethod
    def frames(self) -> int: ...


TickCallback = Callable[[Instrument, Any, float], None]  # (instrument, tick, ltt_epoch)
PrevCloseCallback = Callable[[Instrument, float], None]
EventCallback = Callable[[str, dict], None]


class Platform(ABC):
    name: str = "platform"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def set_token(self, token: str) -> None: ...

    @abstractmethod
    async def resolve(self, items: list[WatchItem]) -> tuple[list[tuple[WatchItem, Instrument]], list[str]]:
        """Map watchlist entries to instruments. Returns (resolved, problems)."""

    @abstractmethod
    def stream(self, instruments: list[tuple[WatchItem, Instrument]], on_tick: TickCallback,
               on_prev_close: PrevCloseCallback, on_event: EventCallback, *,
               on_raw: Callable[[bytes], None] | None = None) -> Stream:
        """``on_raw`` (optional) receives every raw feed frame before parsing, e.g. to record it for replay."""

    @abstractmethod
    async def positions(self) -> list[dict]: ...

    @abstractmethod
    async def holdings(self) -> list[dict]: ...

    @abstractmethod
    async def funds(self) -> list[dict]: ...

    @abstractmethod
    async def orders(self) -> list[dict]: ...

    @abstractmethod
    async def trades_today(self) -> list[dict]: ...

    @abstractmethod
    async def trade_history(self, start: date, end: date) -> list[dict]: ...

    @abstractmethod
    async def ledger(self, start: date, end: date) -> list[dict]: ...

    @abstractmethod
    async def daily_candles(self, inst: Instrument, start: date, end: date) -> list[dict]: ...

    @abstractmethod
    def instrument_for(self, segment: str, security_id: int) -> Instrument | None: ...

    @abstractmethod
    def lookup(self, segment: str, symbol: str) -> Instrument | None: ...
