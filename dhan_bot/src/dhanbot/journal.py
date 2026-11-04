"""SQLite trade journal (WAL mode), written off the hot path.

Callers enqueue rows; a flusher task writes them in a dedicated single-thread executor every 250 ms
(or when 500 rows are pending). Nothing on the tick path ever touches the disk.

Tables: runs, orders, fills, events, pnl, latency.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import orjson

from .models import Fill, Order

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, mode TEXT, version TEXT,
    config_hash TEXT, ended REAL);
CREATE TABLE IF NOT EXISTS orders (local_id INTEGER, run_id INTEGER, cid TEXT PRIMARY KEY, broker_id TEXT,
    strategy TEXT, symbol TEXT, segment TEXT, security_id INTEGER, side TEXT, qty INTEGER, order_type TEXT,
    product TEXT, kind TEXT, intent TEXT, role TEXT, price REAL, trigger REAL, target REAL, stop REAL,
    status TEXT, filled_qty INTEGER, avg_price REAL, exit_filled_qty INTEGER, reason TEXT, tag TEXT,
    created REAL, updated REAL, sent_to_ack_us REAL);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, cid TEXT, symbol TEXT,
    side TEXT, qty INTEGER, price REAL, leg TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts REAL, level TEXT,
    kind TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS pnl (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts REAL, realised REAL,
    unrealised REAL, charges REAL, net REAL, open_positions INTEGER);
CREATE TABLE IF NOT EXISTS latency (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts REAL, stage TEXT,
    n INTEGER, p50_us REAL, p95_us REAL, p99_us REAL, max_us REAL);
CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created);
CREATE INDEX IF NOT EXISTS ix_fills_ts ON fills(ts);
"""


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journal")
        self._conn: sqlite3.Connection | None = None
        self._pending: list[tuple[str, tuple]] = []
        self.run_id = 0
        self._task: asyncio.Task | None = None
        self._closed = False

    # ---- thread side -------------------------------------------------------------------------------
    def _open(self) -> None:
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def _write(self, rows: list[tuple[str, tuple]]) -> None:
        assert self._conn is not None
        c = self._conn
        c.execute("BEGIN")
        try:
            for sql, params in rows:
                c.execute(sql, params)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def _query(self, sql: str, params: tuple) -> list[tuple]:
        assert self._conn is not None
        return self._conn.execute(sql, params).fetchall()

    # ---- async side -----------------------------------------------------------------------------------
    async def open(self, mode: str, version: str, config_hash: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, self._open)

        def start_run() -> int:
            assert self._conn is not None
            cur = self._conn.execute("INSERT INTO runs(started, mode, version, config_hash) VALUES (?,?,?,?)",
                                     (time.time(), mode, version, config_hash))
            return int(cur.lastrowid)

        self.run_id = await loop.run_in_executor(self._pool, start_run)
        self._task = asyncio.create_task(self._flusher())

    async def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._query, sql, params)

    def _enqueue(self, sql: str, params: tuple) -> None:
        if not self._closed:
            self._pending.append((sql, params))

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            await self.flush()

    async def flush(self) -> None:
        if not self._pending or self._conn is None:
            return
        rows, self._pending = self._pending, []
        try:
            await asyncio.get_running_loop().run_in_executor(self._pool, self._write, rows)
        except Exception:
            log.exception("journal write failed (%d rows dropped)", len(rows))

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._enqueue("UPDATE runs SET ended=? WHERE id=?", (time.time(), self.run_id))
        await self.flush()
        self._closed = True
        if self._conn is not None:
            await asyncio.get_running_loop().run_in_executor(self._pool, self._conn.close)
        self._pool.shutdown(wait=True)

    # ---- records ----------------------------------------------------------------------------------------
    def order(self, o: Order) -> None:
        if not o.cid:
            return
        rtt = (o.ack_ns - o.sent_ns) / 1000.0 if o.ack_ns and o.sent_ns else None
        self._enqueue(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(cid) DO UPDATE SET broker_id=excluded.broker_id, status=excluded.status, "
            "filled_qty=excluded.filled_qty, avg_price=excluded.avg_price, exit_filled_qty=excluded.exit_filled_qty, "
            "reason=excluded.reason, price=excluded.price, trigger=excluded.trigger, updated=excluded.updated, "
            "sent_to_ack_us=COALESCE(excluded.sent_to_ack_us, orders.sent_to_ack_us)",
            (o.local_id, self.run_id, o.cid, o.broker_id, o.strategy, o.instrument.trading_symbol, o.instrument.segment,
             o.instrument.security_id, o.side.value, o.qty, o.order_type.value, o.product.value, o.kind.value,
             o.intent.value, o.role, o.price_paise / 100, o.trigger_paise / 100, o.target_paise / 100,
             o.sl_paise / 100, o.status.value, o.filled_qty, round(o.avg_fill_paise / 100, 4), o.exit_filled_qty,
             o.reason, o.tag, time.time(), time.time(), rtt))

    def fill(self, f: Fill, o: Order) -> None:
        self._enqueue("INSERT INTO fills(run_id, cid, symbol, side, qty, price, leg, ts) VALUES (?,?,?,?,?,?,?,?)",
                      (self.run_id, o.cid, f.instrument.trading_symbol, f.side.value, f.qty,
                       round(f.price_paise / 100, 4), f.leg, f.ts))

    def event(self, kind: str, payload: Any = None, level: str = "INFO", ts: float | None = None) -> None:
        self._enqueue("INSERT INTO events(run_id, ts, level, kind, payload) VALUES (?,?,?,?,?)",
                      (self.run_id, ts or time.time(), level, kind,
                       orjson.dumps(payload, default=str).decode() if payload is not None else None))

    def pnl(self, ts: float, realised: float, unrealised: float, charges: float, net: float, open_pos: int) -> None:
        self._enqueue("INSERT INTO pnl(run_id, ts, realised, unrealised, charges, net, open_positions) "
                      "VALUES (?,?,?,?,?,?,?)", (self.run_id, ts, realised, unrealised, charges, net, open_pos))

    def latency(self, ts: float, stats: list) -> None:
        for s in stats:
            self._enqueue("INSERT INTO latency(run_id, ts, stage, n, p50_us, p95_us, p99_us, max_us) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (self.run_id, ts, s.stage, s.n, s.p50_us, s.p95_us, s.p99_us, s.max_us))

    async def max_cid_seq(self, prefix_day: str) -> int:
        rows = await self.query("SELECT cid FROM orders WHERE cid LIKE ? ORDER BY cid DESC LIMIT 1", (prefix_day + "%",))
        if not rows:
            return 0
        try:
            return int(rows[0][0][len(prefix_day):], 36)
        except ValueError:
            return 0
