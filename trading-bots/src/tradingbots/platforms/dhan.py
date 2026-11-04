"""Dhan adapter, built on the tested ``dhanbot`` connectors (binary feed parser, feed socket, REST
client, instrument master). The "simulated" platform is this same adapter pointed at a local mock Dhan.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path
from typing import Callable

from dhanbot.clock import LttNormalizer, RealClock
from dhanbot.config import load_config
from dhanbot.dhan.endpoints import MODE_CODES, Endpoints
from dhanbot.dhan.feed import FeedConnection
from dhanbot.dhan.packets import PrevClose, Tick, parse_frame
from dhanbot.dhan.rest import DhanRest, Result
from dhanbot.instruments import InstrumentStore
from dhanbot.models import Instrument
from dhanbot.ratelimit import AsyncLimiter

from ..core.settings import WatchItem
from . import normalize as N
from .base import EventCallback, Platform, PlatformError, PrevCloseCallback, Stream, TickCallback

log = logging.getLogger(__name__)


class DhanStream(Stream):
    def __init__(self, feed: FeedConnection) -> None:
        self.feed = feed

    async def run(self) -> None:
        await self.feed.run()

    async def stop(self) -> None:
        await self.feed.stop()

    async def recycle(self) -> None:
        await self.feed.recycle()

    @property
    def connected(self) -> bool:
        return self.feed.connected

    @property
    def frames(self) -> int:
        return self.feed.frames


class DhanPlatform(Platform):
    def __init__(self, name: str, endpoints: Endpoints, client_id: str, token: str, data_dir: Path,
                 clock: RealClock | None = None, *, segments: set[str] | None = None,
                 non_trading_per_sec: int = 8, data_per_sec: int = 2) -> None:
        self.name = name
        self.ep = endpoints
        self.client_id = client_id
        self.token = token
        self.data_dir = Path(data_dir)
        self.clock = clock or RealClock()
        self.segments = segments or {"NSE_EQ", "BSE_EQ", "NSE_FNO", "IDX_I"}
        self.rest = DhanRest(endpoints, client_id, token, limiters={
            "non_trading": AsyncLimiter(non_trading_per_sec), "data": AsyncLimiter(data_per_sec),
            "quote": AsyncLimiter(1)})
        self.store: InstrumentStore | None = None
        self.ltt = LttNormalizer("auto", self.clock)

    # ---- lifecycle -----------------------------------------------------------------------------
    async def start(self) -> None:
        await self.rest.start()
        cfg = load_config(data={"paths": {"data_dir": str(self.data_dir)}}, env={})
        today = self.clock.today()
        self.store = await InstrumentStore.load(cfg, self.ep.instruments_csv_url, self.segments, today)

    async def close(self) -> None:
        await self.rest.close()

    def set_token(self, token: str) -> None:
        self.token = token
        self.rest.set_token(token)

    # ---- instruments ------------------------------------------------------------------------------
    def instrument_for(self, segment: str, security_id: int) -> Instrument | None:
        return self.store.get(segment, security_id) if self.store else None

    def lookup(self, segment: str, symbol: str) -> Instrument | None:
        return self.store.by_trading_symbol(segment, symbol) if self.store else None

    async def resolve(self, items: list[WatchItem]) -> tuple[list[tuple[WatchItem, Instrument]], list[str]]:
        if self.store is None:
            raise PlatformError("platform not started")
        out: list[tuple[WatchItem, Instrument]] = []
        problems: list[str] = []
        for w in items:
            inst = None
            if w.security_id:
                inst = self.store.get(w.segment, w.security_id)
                if inst is None and w.segment == "IDX_I":
                    inst = Instrument(w.security_id, "IDX_I", 0, w.symbol, name=w.symbol, instrument_type="INDEX")
            if inst is None:
                inst = self.store.by_trading_symbol(w.segment, w.symbol)
            if inst is None:
                hints = [i.trading_symbol for i in self.store.search(w.symbol, limit=3)]
                problems.append(f"{w.segment}:{w.symbol} not found on {self.name}" + (f" (did you mean {hints}?)"
                                                                                    if hints else ""))
                continue
            out.append((w, inst))
        return out, problems

    # ---- live data -------------------------------------------------------------------------------------
    def stream(self, instruments: list[tuple[WatchItem, Instrument]], on_tick: TickCallback,
               on_prev_close: PrevCloseCallback, on_event: EventCallback, *,
               on_raw: Callable[[bytes], None] | None = None) -> Stream:
        by_key = {inst.key: inst for _, inst in instruments}
        ltt = self.ltt

        def on_frame(data: bytes) -> None:
            if on_raw is not None:
                on_raw(data)
            for p in parse_frame(data):
                cls = p.__class__
                if cls is Tick:
                    inst = by_key.get((p.seg, p.sid))
                    if inst is None:
                        continue
                    if not ltt.detected:
                        ltt.observe(p.ltt)
                    on_tick(inst, p, ltt.to_epoch(p.ltt))
                elif cls is PrevClose:
                    inst = by_key.get((p.seg, p.sid))
                    if inst is not None:
                        on_prev_close(inst, p.prev_close)

        feed = FeedConnection(f"{self.name}-feed", lambda: self.ep.feed_ws_url(self.client_id, self.token), on_frame,
                              on_event=on_event)
        for w, inst in instruments:
            feed.subscribe([(inst.segment, inst.security_id)], MODE_CODES[w.mode])
        return DhanStream(feed)

    # ---- account -----------------------------------------------------------------------------------------
    @staticmethod
    def _rows(res: Result, what: str) -> list[dict]:
        if not res.ok:
            raise PlatformError(f"{what}: {res.error}")
        data = res.data
        if isinstance(data, dict):
            data = data.get("data", [data])
        return [r for r in (data or []) if isinstance(r, dict)]

    async def positions(self) -> list[dict]:
        now = time.time()
        return [N.position_row(r, self.name, now) for r in self._rows(await self.rest.get_positions(), "positions")]

    async def holdings(self) -> list[dict]:
        res = await self.rest.get_holdings()
        if not res.ok and res.error and "no holding" in (res.error.message or "").lower():
            return []
        now = time.time()
        return [N.holding_row(r, self.name, now) for r in self._rows(res, "holdings")]

    async def funds(self) -> list[dict]:
        now = time.time()
        return [N.funds_row(r, self.name, now) for r in self._rows(await self.rest.get_funds(), "funds")]

    async def orders(self) -> list[dict]:
        return [N.order_row(r, self.name) for r in self._rows(await self.rest.get_orders(), "orders")]

    async def trades_today(self) -> list[dict]:
        return [N.trade_row(r, self.name) for r in self._rows(await self.rest.get_trades(), "trades")]

    async def trade_history(self, start: date, end: date) -> list[dict]:
        out: list[dict] = []
        for page in range(0, 200):
            rows = self._rows(await self.rest.get_trade_history(start.isoformat(), end.isoformat(), page), "trade history")
            out.extend(N.trade_row(r, self.name) for r in rows)
            if len(rows) < 50:  # Dhan pages are fixed-size; a short page is the last one
                break
        return out

    async def ledger(self, start: date, end: date) -> list[dict]:
        return [N.ledger_row(r, self.name) for r in
                self._rows(await self.rest.get_ledger(start.isoformat(), end.isoformat()), "ledger")]

    async def daily_candles(self, inst: Instrument, start: date, end: date) -> list[dict]:
        res = await self.rest.historical_daily(inst.security_id, inst.segment, inst.instrument_type or "EQUITY",
                                               start.isoformat(), end.isoformat())
        if not res.ok:
            raise PlatformError(f"daily candles {inst.trading_symbol}: {res.error}")
        return N.candle_rows(res.data if isinstance(res.data, dict) else {})
