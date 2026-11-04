"""History bot: daily-candle history and per-stock trends.

Flow (the bots working together): history asks market-data for the missing candles
(``cmd.market_data.fetch_history``) -> market-data publishes ``market.history`` -> the tables bot stores
``candles_daily`` and announces ``tables.updated`` -> history recomputes ``history_metrics``,
``monthly_returns`` and ``correlations`` and publishes ``history.updated``.

Runs daily at ``schedule.history_refresh`` (16:00 IST), at start-up when stale, when the watchlist
changes, and on ``cmd.history.refresh``.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import polars as pl

from ...core import topics
from ...core.bot import Bot
from ...core.bus import BusTimeout, Message
from ...core.settings import Settings
from ...core.tables_io import TableReader, TableStore
from . import metrics as M


class HistoryBot(Bot):
    name = "history"

    def configure(self) -> None:
        self.on(topics.TABLES_UPDATED, self.on_tables)
        self.command("refresh", self.cmd_refresh)
        self.command("recompute", self.cmd_recompute)
        self.daily(lambda: self.settings.schedule.history_refresh, self.refresh, name="daily-refresh")
        self._recompute_pending = False
        self.last_run: dict = {}

    async def setup(self) -> None:
        self.store = TableStore(self.ctx.paths.tables)
        self.reader = TableReader(self.ctx.paths.tables)
        self.spawn(self._startup_refresh(), name="startup-refresh")

    def details(self) -> dict:
        return {"last_run": self.last_run}

    async def _startup_refresh(self) -> None:
        await asyncio.sleep(3.0)  # let market-data connect first
        try:
            m = self.reader.query("SELECT MAX(as_of) AS a FROM history_metrics")
            last = m["a"][0] if m.height else None
        except Exception:
            last = None
        if last is None or last < self.ctx.calendar.last_trading_day(self.ctx.today() - timedelta(days=1)):
            await self.refresh()

    async def on_settings(self, old: Settings, new: Settings) -> None:
        if old.watchlist != new.watchlist:
            self.spawn(self.refresh(), name="watchlist-refresh")

    def symbols(self) -> list[dict]:
        out = {w.symbol: {"symbol": w.symbol, "segment": w.segment, "security_id": w.security_id}
               for w in self.settings.watchlist}
        try:
            held = self.reader.query("SELECT DISTINCT symbol, security_id FROM holdings WHERE date = "
                                     "(SELECT MAX(date) FROM holdings)")
            for r in held.to_dicts():
                out.setdefault(r["symbol"], {"symbol": r["symbol"], "segment": "NSE_EQ",
                                             "security_id": int(r["security_id"] or 0)})
        except Exception:
            pass
        return list(out.values())

    async def refresh(self, symbols: list[dict] | None = None) -> dict:
        syms = symbols or self.symbols()
        today = self.ctx.today()
        default_start = today - timedelta(days=int(self.settings.schedule.history_years * 366))
        try:
            last = {r["symbol"]: r["last"] for r in
                    self.reader.query("SELECT symbol, MAX(day) AS last FROM candles_daily GROUP BY symbol").to_dicts()}
        except Exception:
            last = {}
        groups: dict[date, list[dict]] = {}
        for s in syms:
            start = (last[s["symbol"]] + timedelta(days=1)) if s["symbol"] in last else default_start
            if start < today:
                groups.setdefault(start, []).append(s)
        fetched: dict[str, int] = {}
        errors: dict[str, str] = {}
        for start, items in groups.items():
            try:
                r = await self.bus.request(topics.cmd("market_data", "fetch_history"),
                                           {"symbols": items, "from": start.isoformat(), "to": today.isoformat()},
                                           timeout=120)
                fetched.update(r.get("fetched", {}))
                errors.update(r.get("errors", {}))
            except BusTimeout as e:
                errors["*"] = str(e)
        self.last_run = {"at": self.clock.time(), "fetched": fetched, "errors": errors}
        if errors:
            await self.alert(f"history fetch problems: {errors}", "WARNING", key="history-fetch")
        if not any(fetched.values()):
            await self.recompute()  # nothing new arrived, but metrics may still be missing
        return self.last_run

    async def on_tables(self, msg: Message) -> None:
        if msg.data.get("table") == "candles_daily" and not self._recompute_pending:
            self._recompute_pending = True
            self.spawn(self._debounced(), name="recompute")

    async def _debounced(self) -> None:
        await asyncio.sleep(1.0)  # coalesce a burst of per-symbol updates
        self._recompute_pending = False
        await self.recompute()

    async def recompute(self) -> dict:
        res = await asyncio.to_thread(self._compute)
        if res["symbols"]:
            await self.publish(topics.HISTORY_UPDATED, res)
        return res

    def _compute(self) -> dict:
        df = self.reader.query("SELECT symbol, day, open, high, low, close, volume FROM candles_daily ORDER BY symbol, day")
        if df.height == 0:
            return {"symbols": [], "as_of": None}
        bench_item = self.settings.benchmark()
        series: dict[str, dict[date, float]] = {}
        frames = {sym: part for (sym,), part in df.partition_by("symbol", as_dict=True).items()}
        bench = None
        if bench_item and bench_item.symbol in frames:
            b = frames[bench_item.symbol]
            bench = dict(zip(b["day"].to_list(), b["close"].to_list(), strict=True))
        rows, monthly = [], []
        for sym, part in frames.items():
            if part.height < 2:
                continue
            days = part["day"].to_list()
            closes = part["close"].to_list()
            rows.append(M.stock_metrics(sym, days, part["open"].to_list(), part["high"].to_list(),
                                        part["low"].to_list(), closes, part["volume"].to_list(),
                                        None if bench_item and sym == bench_item.symbol else bench))
            monthly.extend(M.monthly_returns(sym, days, closes))
            if not (bench_item and sym == bench_item.symbol):
                series[sym] = dict(zip(days, closes, strict=True))
        self.store.replace("history_metrics", rows)
        self.store.replace("monthly_returns", monthly)
        self.store.replace("correlations", M.correlations(series))
        as_of = max(r["as_of"] for r in rows).isoformat() if rows else None
        return {"symbols": sorted(r["symbol"] for r in rows), "as_of": as_of}

    async def cmd_refresh(self, data: dict) -> dict:
        return await self.refresh(data.get("symbols"))

    async def cmd_recompute(self, data: dict) -> dict:
        return await self.recompute()


def metrics_frame(rows: list[dict]) -> pl.DataFrame:  # helper for notebooks / tests
    from ...core.schemas import coerce

    return coerce("history_metrics", rows)
