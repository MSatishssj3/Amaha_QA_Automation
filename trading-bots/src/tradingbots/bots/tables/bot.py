"""Tables bot: turns everything market-data and execution publish into neat, typed Parquet tables
(see ``core/schemas.py``) that the analysis bots, the AI and the dashboard query with DuckDB.

* Append tables (``bars_1m``, ``funds``, ``exec_fills``) are buffered and flushed every 2 s; parts are
  compacted nightly.
* Snapshot tables (``positions``, ``holdings``, ``orders``, ``trades``, ``ledger``, ``exec_orders``)
  replace their date partition, so re-delivered messages never create duplicates.
* ``candles_daily`` merges new candles into each symbol's partition (deduplicated by day).
* Orders and trades are tagged ``source = bot | manual`` from the execution bot's order ids.
* After each write: ``tables.updated {table, rows, partitions}``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import polars as pl
from dhanbot.clock import ist_datetime

from ...core import topics
from ...core.bot import Bot
from ...core.bus import Message
from ...core.tables_io import TableReader, TableStore

APPEND_TABLES = ("bars_1m", "funds", "exec_fills", "ai_costs")


def _day(v: Any) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


class TablesBot(Bot):
    name = "tables"
    start_from = "earliest"  # first run: take whatever is still on the bus

    def configure(self) -> None:
        self.on(topics.MARKET_QUOTES, self.on_quotes)
        self.on(topics.MARKET_BARS, self.on_bars)
        self.on(topics.MARKET_HISTORY, self.on_history)
        for t in topics.ACCOUNT_TOPICS:
            self.on(t, self.on_account)
        self.on(topics.EXEC_ORDER, self.on_exec_order)
        self.on(topics.EXEC_FILL, self.on_exec_fill)
        self.command("list", self.cmd_list)
        self.command("flush", self.cmd_flush)
        self.command("compact", self.cmd_compact)
        self.every(2.0, self.flush, name="flush", wall_clock=True)
        self.every(5.0, self.write_quotes, name="quotes", wall_clock=True)
        self.daily(lambda: self.settings.schedule.compaction, self.compact_all, name="compaction",
                   trading_days_only=False)
        self.quotes: dict[str, dict] = {}
        self._quotes_dirty = False
        self._buffers: dict[str, list[dict]] = defaultdict(list)
        self.exec_orders: dict[str, dict] = {}
        self.bot_order_ids: set[str] = set()
        self.rows_written: dict[str, int] = defaultdict(int)

    async def setup(self) -> None:
        self.store = TableStore(self.ctx.paths.tables)
        self.reader = TableReader(self.ctx.paths.tables)
        today = self.ctx.today()
        try:  # resume today's execution-order state and remember which broker orders are ours
            df = self.reader.query("SELECT * FROM exec_orders WHERE date >= ?", [today - timedelta(days=60)])
            for r in df.to_dicts():
                if r.get("broker_id"):
                    self.bot_order_ids.add(r["broker_id"])
                if r.get("date") == today:
                    r.pop("date", None)
                    self.exec_orders[r["cid"]] = r
        except Exception as e:
            self.log.warning("could not reload exec_orders: %s", e)

    async def teardown(self) -> None:
        await self.flush()
        await self.write_quotes()

    def details(self) -> dict:
        return {"rows_written": dict(self.rows_written), "buffered": {k: len(v) for k, v in self._buffers.items()}}

    async def _updated(self, table: str, rows: int, partitions: list[str]) -> None:
        self.rows_written[table] += rows
        await self.publish(topics.TABLES_UPDATED, {"table": table, "rows": rows, "partitions": partitions})

    def _today(self) -> date:
        return self.ctx.today()

    # ---- market -----------------------------------------------------------------------------
    async def on_quotes(self, msg: Message) -> None:
        for q in msg.data.get("quotes", []):
            self.quotes[f"{q.get('segment')}:{q.get('symbol')}"] = q
        self._quotes_dirty = True

    async def write_quotes(self) -> None:
        if not self._quotes_dirty:
            return
        self._quotes_dirty = False
        rows = list(self.quotes.values())
        await asyncio.to_thread(self.store.replace, "quotes_latest", rows)
        await self._updated("quotes_latest", len(rows), [])

    async def on_bars(self, msg: Message) -> None:
        for b in msg.data.get("bars", []):
            b = dict(b)
            b["date"] = ist_datetime(float(b["start_ts"])).date()
            self._buffers["bars_1m"].append(b)
        if len(self._buffers["bars_1m"]) >= 5000:
            await self.flush()

    async def on_history(self, msg: Message) -> None:
        d = msg.data
        sym = d["symbol"]
        new = [dict(c, segment=d.get("segment"), security_id=d.get("security_id"), day=_day(c.get("day")))
               for c in d.get("candles", [])]
        if not new:
            return
        await asyncio.to_thread(self._merge_candles, sym, new)
        await self._updated("candles_daily", len(new), [sym])

    def _merge_candles(self, symbol: str, new: list[dict]) -> None:
        try:
            old = self.reader.query("SELECT * EXCLUDE (symbol) FROM candles_daily WHERE symbol = ?", [symbol])
        except Exception:
            old = pl.DataFrame()
        frames = [pl.from_dicts(new, strict=False)]
        if old.height:
            frames.insert(0, old)
        from ...core.schemas import coerce

        merged = pl.concat([coerce("candles_daily", f) for f in frames], how="vertical_relaxed")
        merged = merged.unique(subset=["day"], keep="last").sort("day")
        self.store.replace("candles_daily", merged, partition_value=symbol)

    # ---- account ---------------------------------------------------------------------------------
    async def on_account(self, msg: Message) -> None:
        d = msg.data
        rows = [dict(r) for r in d.get("rows", [])]
        kind = d.get("kind") or msg.topic.split(".", 1)[1]
        day = _day(d.get("date")) or self._today()
        if msg.topic == topics.ACCOUNT_POSITIONS:
            await self._replace("positions", rows, day)
        elif msg.topic == topics.ACCOUNT_HOLDINGS:
            await self._replace("holdings", rows, day)
        elif msg.topic == topics.ACCOUNT_FUNDS:
            self._buffers["funds"].extend(dict(r, date=day) for r in rows)
        elif msg.topic == topics.ACCOUNT_ORDERS:
            for r in rows:
                r["source"] = "bot" if r.get("order_id") in self.bot_order_ids else "manual"
            await self._replace("orders", rows, day)
        elif msg.topic == topics.ACCOUNT_TRADES:
            for r in rows:
                r["source"] = "bot" if r.get("order_id") in self.bot_order_ids else "manual"
            by_day: dict[date, list[dict]] = defaultdict(list)
            for r in rows:
                rd = _day(r.pop("date", None))
                if rd is not None:
                    by_day[rd].append(r)
            dates = {_day(x) for x in d.get("dates", [])} | set(by_day)
            for dd in sorted(x for x in dates if x is not None):
                await self._replace("trades", by_day.get(dd, []), dd, event=False)
            await self._updated("trades", len(rows), [str(x) for x in sorted(x for x in dates if x)])
        elif msg.topic == topics.ACCOUNT_LEDGER:
            by_day = defaultdict(list)
            for r in rows:
                rd = _day(r.pop("date", None))
                if rd is not None:
                    by_day[rd].append(r)
            start, end = _day(d.get("from")), _day(d.get("to"))
            days = set(by_day)
            existing = {_day(p) for p in self.store.partitions("ledger")}
            if start and end:
                days |= {x for x in existing if x and start <= x <= end}
            for dd in sorted(days):
                await self._replace("ledger", by_day.get(dd, []), dd, event=False)
            await self._updated("ledger", len(rows), [str(x) for x in sorted(days)])
        else:
            self.log.debug("ignoring %s (%s)", msg.topic, kind)

    async def _replace(self, table: str, rows: list[dict], day: date, event: bool = True) -> None:
        for r in rows:
            r.pop("date", None)
        await asyncio.to_thread(self.store.replace, table, rows, day)
        if event:
            await self._updated(table, len(rows), [day.isoformat()])

    # ---- execution ---------------------------------------------------------------------------------------
    async def on_exec_order(self, msg: Message) -> None:
        o = dict(msg.data)
        if o.get("broker_id"):
            self.bot_order_ids.add(o["broker_id"])
        if not o.get("cid"):
            return
        self.exec_orders[o["cid"]] = o
        await self._replace("exec_orders", list(self.exec_orders.values()), self._today())

    async def on_exec_fill(self, msg: Message) -> None:
        f = dict(msg.data)
        f["date"] = ist_datetime(float(f.get("ts") or self.clock.time())).date()
        self._buffers["exec_fills"].append(f)

    # ---- flushing / maintenance -----------------------------------------------------------------------
    async def flush(self) -> None:
        for table in list(self._buffers):
            rows, self._buffers[table] = self._buffers[table], []
            if not rows:
                continue
            parts = await asyncio.to_thread(self.store.append, table, rows)
            await self._updated(table, len(rows), parts)

    async def compact_all(self) -> dict:
        today = self._today().isoformat()
        merged = 0
        for table in APPEND_TABLES:
            for p in self.store.partitions(table):
                if p < today:
                    merged += await asyncio.to_thread(self.store.compact, table, p)
        self.log.info("compaction merged %d part files", merged)
        return {"merged_parts": merged}

    async def cmd_flush(self, data: dict) -> dict:
        await self.flush()
        self._quotes_dirty = True
        await self.write_quotes()
        return {"flushed": True}

    async def cmd_compact(self, data: dict) -> dict:
        return await self.compact_all()

    async def cmd_list(self, data: dict) -> dict:
        def count() -> dict:
            out = {}
            for t in self.reader.available():
                out[t] = {"rows": self.reader.count(t), "partitions": len(self.store.partitions(t))}
            return out

        return {"tables": await asyncio.to_thread(count)}
