"""The analyst's own records (SQLite): every API call with its cost, every report, open batches."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import orjson

from .prompt import json_default


class AIStore:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        for ddl in (
            "CREATE TABLE IF NOT EXISTS calls (ts REAL, day TEXT, purpose TEXT, model TEXT, served_by TEXT, "
            "stop_reason TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, usd REAL, request_id TEXT, error TEXT)",
            "CREATE INDEX IF NOT EXISTS calls_day ON calls(day)",
            "CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, ts REAL, day TEXT, kind TEXT, status TEXT, "
            "model TEXT, served_by TEXT, headline TEXT, report TEXT, markdown TEXT, packet TEXT, usd REAL, "
            "error TEXT, provider TEXT)",
            "CREATE INDEX IF NOT EXISTS reports_ts ON reports(ts)",
            "CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, ts REAL, day TEXT, symbols TEXT, status TEXT, "
            "counts TEXT)",
        ):
            self.db.execute(ddl)

    def close(self) -> None:
        self.db.close()

    # ---- cost log ---------------------------------------------------------------------------------------
    def add_call(self, *, ts: float, day: str, purpose: str, model: str, served_by: str, stop_reason: str,
                 usage: dict, usd: float, request_id: str = "", error: str = "") -> None:
        self.db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ts, day, purpose, model, served_by, stop_reason, usage.get("input_tokens", 0),
                         usage.get("output_tokens", 0), usage.get("cache_read_tokens", 0),
                         usage.get("cache_write_tokens", 0), usd, request_id, error))

    def spent(self, day: str) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(usd), 0) FROM calls WHERE day = ?", (day,)).fetchone()[0])

    def avg_output_tokens(self, purpose_like: str, n: int = 10) -> float | None:
        """Average output tokens of the last ``n`` successful calls whose purpose matches (SQL LIKE)."""
        rows = self.db.execute("SELECT output_tokens FROM calls WHERE purpose LIKE ? AND error = '' "
                               "ORDER BY ts DESC LIMIT ?", (purpose_like, n)).fetchall()
        return sum(r[0] for r in rows) / len(rows) if rows else None

    def calls(self, since_day: str) -> list[dict]:
        cur = self.db.execute("SELECT * FROM calls WHERE day >= ? ORDER BY ts DESC", (since_day,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    # ---- reports ------------------------------------------------------------------------------------------
    def add_report(self, *, ts: float, day: str, kind: str, status: str, model: str, served_by: str, headline: str,
                   report: dict | None, markdown: str, packet: dict, usd: float, error: str = "",
                   provider: str = "anthropic") -> str:
        rid = f"{day}-{kind}-{uuid.uuid4().hex[:8]}"
        self.db.execute("INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, ts, day, kind, status, model, served_by, headline,
                         orjson.dumps(report, default=json_default).decode() if report is not None else None, markdown,
                         orjson.dumps(packet, default=json_default).decode(), usd, error, provider))
        return rid

    def reports(self, limit: int = 20, kind: str | None = None) -> list[dict]:
        q = "SELECT id, ts, day, kind, status, model, served_by, headline, usd, error, provider FROM reports"
        args: tuple = ()
        if kind:
            q += " WHERE kind = ?"
            args = (kind,)
        cur = self.db.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def report(self, rid: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM reports WHERE id = ?", (rid,))
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(zip([c[0] for c in cur.description], row, strict=True))
        for k in ("report", "packet"):
            d[k] = orjson.loads(d[k]) if d.get(k) else None
        return d

    # ---- batches ------------------------------------------------------------------------------------------------
    def add_batch(self, bid: str, ts: float, day: str, symbols: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?,?)",
                        (bid, ts, day, orjson.dumps(symbols).decode(), "in_progress", "{}"))

    def open_batches(self) -> list[dict]:
        rows = self.db.execute("SELECT id, ts, day, symbols FROM batches WHERE status = 'in_progress'").fetchall()
        return [{"id": r[0], "ts": r[1], "day": r[2], "symbols": orjson.loads(r[3])} for r in rows]

    def close_batch(self, bid: str, status: str, counts: dict) -> None:
        self.db.execute("UPDATE batches SET status = ?, counts = ? WHERE id = ?", (status, orjson.dumps(counts).decode(),
                                                                                   bid))
