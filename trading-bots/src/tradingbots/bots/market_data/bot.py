"""Market-data bot: the team's single connection to the trading platform (besides execution).

* Live feed for the watchlist: every tick updates the latest quote and the 1-minute candles (exact,
  built from every tick by dhanbot's ``BarAggregator``). Quotes are coalesced and published every
  ``quote_interval_ms`` as one ``market.quotes`` message; closed candles go out as ``market.bars``.
* Account snapshots on a timer: positions, holdings, funds, orders, today's trades; the ledger and the
  trade history (with charges) at start-up, every 30 minutes and after the close.
* ``cmd.market_data.fetch_history``: daily candles for symbols, published as ``market.history``.
* Hot platform switch: when settings change platform / watchlist, it reconnects.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from typing import Any

from dhanbot.bars import BarAggregator, parse_timeframe
from dhanbot.clock import ist_datetime
from dhanbot.recorder import TickRecorder
from dhanbot.models import Instrument

from ...core import topics
from ...core.bot import Bot
from ...core.settings import Settings, WatchItem
from ...platforms import Platform, PlatformError, make_platform
from ...platforms.base import Stream


def _quote_key(inst: Instrument) -> str:
    return f"{inst.segment}:{inst.trading_symbol}"


class MarketDataBot(Bot):
    name = "market_data"

    def configure(self) -> None:
        self.on(topics.AUTH_TOKEN, self.on_token)
        self.command("fetch_history", self.cmd_fetch_history)
        self.command("snapshot", self.cmd_snapshot)
        self.command("fetch_trades", self.cmd_fetch_trades, background=True)
        self.platform: Platform | None = None
        self.stream: Stream | None = None
        self.instruments: list[tuple[WatchItem, Instrument]] = []
        self.quotes: dict[str, dict[str, Any]] = {}
        self._dirty: set[str] = set()
        self._bars_out: list[dict] = []
        self._prev_close: dict[tuple[int, int], float] = {}
        self.bars: BarAggregator | None = None
        self._loops: list[asyncio.Task] = []
        self._token_version = 0
        self.problems: list[str] = []
        self.snapshots = 0
        self.recorder: TickRecorder | None = None
        self._rec_day = ""
        self._last_ledger = 0.0
        self._lock = asyncio.Lock()

    def details(self) -> dict:
        return {"platform": self.platform.name if self.platform else None, "instruments": len(self.instruments),
                "connected": bool(self.stream and self.stream.connected),
                "frames": self.stream.frames if self.stream else 0, "problems": self.problems[:5],
                "snapshots": self.snapshots}

    async def setup(self) -> None:
        self.spawn(self._connect_when_ready(), name="connect")

    async def teardown(self) -> None:
        await self._disconnect()

    # ---- connection management -----------------------------------------------------------------
    async def _token(self) -> dict | None:
        info = await self.bus.kv_get(topics.KV_TOKEN)
        if info and info.get("platform") == self.settings.platform.name and info.get("token"):
            return info
        return None

    async def _connect_when_ready(self) -> None:
        while not self._stop.is_set():
            info = await self._token()
            if info:
                try:
                    await self._connect(info)
                    return
                except Exception as e:
                    self._error("connect", e)
                    await self.alert(f"market-data could not connect to {self.settings.platform.name}: {e}", "ERROR",
                                     key="md-connect")
            await asyncio.sleep(2.0)

    async def _connect(self, info: dict) -> None:
        async with self._lock:
            await self._disconnect()
            s = self.settings
            segments = {w.segment for w in s.watchlist} | {"NSE_EQ", "IDX_I"}
            self.platform = make_platform(s.platform.name, info, self.ctx.paths.bot("market_data"), self.clock, segments)
            await self.platform.start()
            self._token_version = int(info.get("version", 0))
            self.instruments, self.problems = await self.platform.resolve(s.watchlist)
            for p in self.problems:
                await self.alert(f"watchlist: {p}", "WARNING", key=f"wl:{p}")
            self.bars = BarAggregator(lambda t: self.ctx.calendar.session(ist_datetime(t).date())[0])
            tfs = [parse_timeframe(t) for t in s.market_data.bar_timeframes]
            for _, inst in self.instruments:
                for tf in tfs:
                    self.bars.add(inst.key, inst.trading_symbol, tf)
            self.quotes = {}
            self.stream = self.platform.stream(self.instruments, self._on_tick, self._on_prev_close, self._on_feed_event,
                                               on_raw=self._on_raw)
            self._loops = [asyncio.create_task(self.stream.run(), name="md-feed"),
                           asyncio.create_task(self._publisher(), name="md-publisher"),
                           asyncio.create_task(self._snapshots(), name="md-snapshots")]
            self.log.info("connected to %s: %d instruments", s.platform.name, len(self.instruments))

    async def _disconnect(self) -> None:
        if self.stream is not None:
            await self.stream.stop()
        await self._close_recorder()
        for t in self._loops:
            t.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        self._loops = []
        self.stream = None
        if self.platform is not None:
            await self.platform.close()
            self.platform = None

    async def on_settings(self, old: Settings, new: Settings) -> None:
        if (old.platform != new.platform or old.watchlist != new.watchlist
                or old.market_data.bar_timeframes != new.market_data.bar_timeframes):
            self.log.info("platform/watchlist changed - reconnecting")
            await self._disconnect()
            self.spawn(self._connect_when_ready(), name="reconnect")

    async def on_token(self, msg) -> None:
        info = await self._token()
        if not info:
            return
        if self.platform is None:
            return
        if int(info.get("version", 0)) != self._token_version:
            self._token_version = int(info.get("version", 0))
            self.platform.set_token(info["token"])
            if self.stream is not None:
                await self.stream.recycle()

    # ---- raw recording (market_data.record_raw) --------------------------------------------------------------
    def _on_raw(self, frame: bytes) -> None:
        """Every raw frame goes to data/ticks/<IST day>.bin (dhanbot's replay format), stamped with the team's
        clock so a recorded demo day replays on its own timeline. Follows the setting live, no reconnect needed."""
        if not self.settings.market_data.record_raw:
            if self.recorder is not None:
                self.spawn(self._close_recorder(), name="rec-close")
            return
        now = self.clock.time()
        day = ist_datetime(now).date().isoformat()
        if self.recorder is None or day != self._rec_day:
            old, self._rec_day = self.recorder, day
            self.recorder = TickRecorder(self.ctx.paths.data / "ticks" / f"{day}.bin")
            self.recorder.start()
            if old is not None:
                self.spawn(old.close(), name="rec-rotate")
        self.recorder.add(frame, int(now * 1e9))

    async def _close_recorder(self) -> None:
        rec, self.recorder = self.recorder, None
        if rec is not None:
            await rec.close()

    # ---- hot path: ticks -------------------------------------------------------------------------------
    def _on_prev_close(self, inst: Instrument, prev_close: float) -> None:
        self._prev_close[inst.key] = prev_close
        q = self.quotes.get(_quote_key(inst))
        if q:
            q["prev_close"] = prev_close

    def _on_tick(self, inst: Instrument, t: Any, ltt: float) -> None:
        key = _quote_key(inst)
        pc = self._prev_close.get(inst.key, 0.0)
        now = self.clock.time()
        bid = t.bids[0][0] if getattr(t, "bids", None) else 0.0
        ask = t.asks[0][0] if getattr(t, "asks", None) else 0.0
        q = self.quotes.get(key)
        if q is None:
            q = self.quotes[key] = {"symbol": inst.trading_symbol, "segment": inst.segment,
                                    "security_id": inst.security_id}
        q.update(ltp=t.ltp, bid=bid or q.get("bid", 0.0), ask=ask or q.get("ask", 0.0),
                 volume=t.volume if t.code != 2 else q.get("volume", 0), oi=t.oi or q.get("oi", 0), prev_close=pc,
                 change_pct=round((t.ltp - pc) / pc * 100, 3) if pc else 0.0, ltt_ts=ltt, updated_ts=now)
        self._dirty.add(key)
        if self.bars is not None:
            bt = ltt if ltt and abs(ltt - now) < 600 else now
            for b in self.bars.on_tick(inst.key, bt, t.ltp, t.volume if t.code != 2 else None):
                self._bars_out.append(self._bar_row(inst, b))

    @staticmethod
    def _bar_row(inst: Instrument, b) -> dict:
        return {"symbol": inst.trading_symbol, "segment": inst.segment, "security_id": inst.security_id, "tf": b.tf,
                "start_ts": b.start, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": b.volume}

    def _on_feed_event(self, kind: str, info: dict) -> None:
        if kind in ("fatal", "token_expired", "unstable", "stale"):
            self.spawn(self.alert(f"market feed {kind}: {info}", "ERROR" if kind == "fatal" else "WARNING",
                                  key=f"feed:{kind}"), name="feed-alert")

    async def _publisher(self) -> None:
        last_status = 0.0
        by_key = {inst.key: inst for _, inst in self.instruments}
        while True:
            await asyncio.sleep(self.settings.market_data.quote_interval_ms / 1000.0)
            platform = self.platform.name if self.platform else ""
            if self._dirty:
                keys, self._dirty = self._dirty, set()
                await self.publish(topics.MARKET_QUOTES, {"platform": platform,
                                                          "quotes": [self.quotes[k] for k in keys if k in self.quotes]})
                await self.bus.kv_set(topics.KV_QUOTES, self.quotes)
            if self.bars is not None:
                for b in self.bars.flush(self.clock.time()):
                    inst = by_key.get(b.key)
                    if inst is not None:
                        self._bars_out.append(self._bar_row(inst, b))
            if self._bars_out:
                bars, self._bars_out = self._bars_out, []
                await self.publish(topics.MARKET_BARS, {"platform": platform, "bars": bars})
            if time.monotonic() - last_status > 5:
                last_status = time.monotonic()
                ages = [self.clock.time() - q["updated_ts"] for q in self.quotes.values() if q.get("updated_ts")]
                await self.publish(topics.MARKET_STATUS, {
                    "platform": platform, "connected": bool(self.stream and self.stream.connected),
                    "instruments": len(self.instruments), "frames": self.stream.frames if self.stream else 0,
                    "last_tick_age_s": round(min(ages), 1) if ages else None})

    # ---- account snapshots -----------------------------------------------------------------------------------
    async def _snapshots(self) -> None:
        first = True
        while True:
            in_session = self.ctx.calendar.in_session(self.clock.time(), pre_s=1800, post_s=1800)
            if first or in_session:
                await self.snapshot(full=first or time.monotonic() - self._last_ledger > 1800)
                first = False
            await self.clock.sleep(self.settings.market_data.snapshot_interval_s)

    async def snapshot(self, full: bool = False) -> dict:
        p = self.platform
        if p is None:
            return {"taken": False}
        now = self.clock.time()
        today = ist_datetime(now).date()
        out: dict[str, int] = {}
        for kind, topic, fn in (("positions", topics.ACCOUNT_POSITIONS, p.positions),
                                ("holdings", topics.ACCOUNT_HOLDINGS, p.holdings),
                                ("funds", topics.ACCOUNT_FUNDS, p.funds),
                                ("orders", topics.ACCOUNT_ORDERS, p.orders),
                                ("trades", topics.ACCOUNT_TRADES, p.trades_today)):
            try:
                rows = await fn()
            except PlatformError as e:
                self._error(f"snapshot {kind}", e)
                continue
            out[kind] = len(rows)
            await self.publish(topic, {"platform": p.name, "as_of": now, "date": today.isoformat(), "kind": kind,
                                       "dates": [today.isoformat()] if kind == "trades" else [],
                                       "rows": rows})
        if full:
            self._last_ledger = time.monotonic()
            start = today - timedelta(days=self.settings.market_data.ledger_lookback_days)
            try:
                hist = await p.trade_history(start, today)
                dates = sorted({str(r["date"]) for r in hist if r.get("date")} | {today.isoformat()})
                await self.publish(topics.ACCOUNT_TRADES, {"platform": p.name, "as_of": now, "kind": "trade_history",
                                                           "dates": dates, "rows": hist})
                out["trade_history"] = len(hist)
            except PlatformError as e:
                self._error("trade history", e)
            try:
                led = await p.ledger(start, today)
                await self.publish(topics.ACCOUNT_LEDGER, {"platform": p.name, "as_of": now, "kind": "ledger",
                                                           "from": start.isoformat(), "to": today.isoformat(),
                                                           "rows": led})
                out["ledger"] = len(led)
            except PlatformError as e:
                self._error("ledger", e)
        self.snapshots += 1
        return {"taken": True, **out}

    async def cmd_snapshot(self, data: dict) -> dict:
        return await self.snapshot(full=bool(data.get("full", True)))

    async def cmd_fetch_trades(self, data: dict) -> dict:
        """``{from, to}`` (YYYY-MM-DD): the broker's trade history, fetched in 60-day windows (paced), into the
        ``trades`` table. The tax bot uses it for purchase dates older than the regular snapshots reach."""
        p = self.platform
        if p is None:
            return {"fetched": 0, "error": "not connected to a platform yet"}
        try:
            start, end = date.fromisoformat(str(data["from"])), date.fromisoformat(str(data["to"]))
        except (KeyError, ValueError) as e:
            return {"fetched": 0, "error": f"need from and to as YYYY-MM-DD: {e}"}
        if start > end:
            return {"fetched": 0, "error": "from is after to"}
        total, windows, errors, d = 0, 0, [], start
        while d <= end:
            w_end = min(end, d + timedelta(days=59))
            try:
                rows = await p.trade_history(d, w_end)
            except PlatformError as e:
                errors.append(f"{d}..{w_end}: {e}")
                rows = []
            if rows:
                dates = sorted({str(r["date"]) for r in rows if r.get("date")})
                await self.publish(topics.ACCOUNT_TRADES, {"platform": p.name, "as_of": self.clock.time(),
                                                           "kind": "trade_history", "dates": dates, "rows": rows})
            total, windows, d = total + len(rows), windows + 1, w_end + timedelta(days=1)
            await asyncio.sleep(0.3)  # stay well inside the broker's rate limits
        return {"fetched": total, "windows": windows, "errors": errors, "from": start.isoformat(), "to": end.isoformat()}

    # ---- history on request -------------------------------------------------------------------------------
    async def cmd_fetch_history(self, data: dict) -> dict:
        """``{symbols: [{symbol, segment, security_id}], from: YYYY-MM-DD, to: YYYY-MM-DD}``"""
        p = self.platform
        if p is None:
            return {"fetched": {}, "error": "not connected to a platform yet"}
        from datetime import date as _date

        start, end = _date.fromisoformat(data["from"]), _date.fromisoformat(data["to"])
        counts: dict[str, int] = {}
        errors: dict[str, str] = {}
        for item in data.get("symbols", []):
            seg, sym, sid = item.get("segment", "NSE_EQ"), item["symbol"], int(item.get("security_id") or 0)
            inst = (p.instrument_for(seg, sid) if sid else None) or p.lookup(seg, sym)
            if inst is None and seg == "IDX_I" and sid:
                inst = Instrument(sid, "IDX_I", 0, sym, instrument_type="INDEX")
            if inst is None:
                errors[sym] = "unknown instrument"
                continue
            try:
                candles = await p.daily_candles(inst, start, end)
            except PlatformError as e:
                errors[sym] = str(e)
                continue
            counts[sym] = len(candles)
            await self.publish(topics.MARKET_HISTORY, {"platform": p.name, "symbol": sym, "segment": inst.segment,
                                                       "security_id": inst.security_id, "from": data["from"],
                                                       "to": data["to"], "candles": candles})
        return {"fetched": counts, "errors": errors}
