"""Tax bot: an estimate of the Indian tax on your trades, and a statement for your CA. Not tax advice.

* Matches every sale to its purchase (FIFO, with purchase dates). Each sale is sorted into intraday (speculative),
  F&O (business), short-term or long-term capital gains; ``analytics.py`` lists the rules applied.
* Writes three tables:
  - ``tax_lots``: every matched sale;
  - ``tax_summary``: per financial year, gains, losses, set-off, exemption, taxable amount and estimated tax;
  - ``tax_open_lots``: the shares you hold, with the day each lot turns long-term.
* Purchase dates come from the broker's trade history. On start, the bot asks the market-data bot once to fetch
  that history back to ``tax.history_from`` (default: the start of the previous financial year).
* Shares bought before that have no known date. Add them on the Tax page (``cmd.tax.add_lot``, kept in
  ``data/tax/tax.db``). They are never guessed.
* The rates and your slab are in ``[tax]``. Every number is an estimate to check with your CA.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import date

from ...core import topics
from ...core.bot import Bot
from ...core.bus import BusTimeout, Message
from ...core.settings import Settings
from ...core.tables_io import TableReader, TableStore
from . import analytics as A

WATCHED = {"trades", "holdings"}
CHARGE_COLS = ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst")
HOLDINGS_SQL = ("SELECT platform, symbol, SUM(qty) AS qty FROM holdings WHERE date = (SELECT MAX(date) FROM holdings) "
                "GROUP BY platform, symbol")


def _side(v) -> int:
    s = str(v or "").upper()
    return 1 if s == "BUY" else -1 if s == "SELL" else 0


class TaxBot(Bot):
    name = "tax"

    def configure(self) -> None:
        self.on(topics.TABLES_UPDATED, self.on_tables)
        self.command("recompute", self.cmd_recompute)
        self.command("add_lot", self.cmd_add_lot)
        self.command("delete_lot", self.cmd_delete_lot)
        self.command("lots", self.cmd_lots)
        self.command("backfill", self.cmd_backfill)
        self.command("status", self.cmd_status)
        self.daily("06:30", self._daily, name="daily", trading_days_only=False)
        self.headline: dict = {}
        self._pending = False
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        self.store = TableStore(self.ctx.paths.tables)
        self.reader = TableReader(self.ctx.paths.tables)
        self.db = sqlite3.connect(self.ctx.paths.bot("tax") / "tax.db", isolation_level=None, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS manual_lots (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, "
                        "symbol TEXT, qty INTEGER, buy_date TEXT, price REAL, fmv_2018 REAL, note TEXT, created REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
        self.spawn(self._start(), name="startup")

    async def teardown(self) -> None:
        self.db.close()

    def details(self) -> dict:
        return {**self.headline, "history": self._state()}

    async def on_settings(self, old: Settings, new: Settings) -> None:
        if old.tax != new.tax:
            await self.recompute()
            if old.tax.history_from != new.tax.history_from:
                self.spawn(self.ensure_history(), name="history")

    async def on_tables(self, msg: Message) -> None:
        if msg.data.get("table") in WATCHED and not self._pending:
            self._pending = True
            self.spawn(self._debounced(), name="debounced")

    async def _debounced(self) -> None:
        await asyncio.sleep(3.0)
        self._pending = False
        await self.recompute()

    async def _start(self) -> None:
        await self.recompute()
        if not self.settings.tax.enabled:
            return
        for _ in range(6):  # the market-data bot may still be connecting: try for a few minutes, then daily
            r = await self.ensure_history()
            if r.get("ok_history"):
                return
            await asyncio.sleep(60)

    async def _daily(self) -> None:
        if not self.settings.tax.enabled:
            return
        await self.ensure_history()
        await self.recompute()  # "days to long-term" moves on

    # ---- trade history ---------------------------------------------------------------------------------------
    def _state(self) -> dict:
        row = self.db.execute("SELECT value FROM state WHERE key = 'history'").fetchone()
        return json.loads(row[0]) if row else {}

    def _want_from(self) -> date:
        if self.settings.tax.history_from:
            return date.fromisoformat(self.settings.tax.history_from)
        start, _ = A.fy_range(A.fy_of(self.ctx.today()))
        return date(start.year - 1, 4, 1)

    async def ensure_history(self, force: bool = False) -> dict:
        """Fetch the broker's trade history back to the wanted date, once (the snapshots keep it current)."""
        st, want, today = self._state(), self._want_from(), self.ctx.today()
        covered = st.get("covered_from")
        if not force and covered and date.fromisoformat(covered) <= want:
            return {"ok_history": True, "fetched": False, **st}
        try:
            r = await self.bus.request(topics.cmd("market_data", "fetch_trades"),
                                       {"from": want.isoformat(), "to": today.isoformat()}, timeout=900)
        except BusTimeout:
            r = {"ok": False, "error": "the market_data bot did not answer - is it running?"}
        problem = r.get("error") or "; ".join(r.get("errors") or [])
        st.update(last_try=time.time(), last_error=problem)
        if not problem:
            st.update(covered_from=want.isoformat(), fetched_trades=int(r.get("fetched") or 0), fetched_at=time.time())
        self.db.execute("INSERT OR REPLACE INTO state(key, value) VALUES ('history', ?)", (json.dumps(st),))
        if problem:
            self.log.warning("trade history not fetched: %s", problem)
        return {"ok_history": not problem, "fetched": not problem, **st}

    # ---- computation -----------------------------------------------------------------------------------------
    def _manual(self) -> list[dict]:
        rows = self.db.execute("SELECT id, platform, symbol, qty, buy_date, price, fmv_2018, note FROM manual_lots "
                               "ORDER BY buy_date").fetchall()
        return [{"id": r[0], "platform": r[1], "symbol": r[2], "qty": r[3], "buy_date": r[4], "price": r[5],
                 "fmv_2018": r[6], "note": r[7]} for r in rows]

    def _fills(self) -> list[A.TFill]:
        out, seen = [], set()
        for t in self.reader.query("SELECT * FROM trades").to_dicts():
            tid = str(t.get("trade_id") or f"{t.get('order_id')}@{t.get('trade_ts')}")
            if (t.get("platform"), tid) in seen:
                continue
            seen.add((t.get("platform"), tid))
            stt = float(t.get("stt") or 0)
            total = t.get("total_charges")
            other = float(total) - stt if total is not None else sum(float(t.get(c) or 0) for c in CHARGE_COLS
                                                                   if c != "stt")
            out.append(A.TFill(t.get("platform") or "", t.get("symbol") or "", t.get("segment") or "",
                               t.get("product") or "", tid, float(t.get("trade_ts") or 0), _side(t.get("side")),
                               int(t.get("qty") or 0), float(t.get("price") or 0), stt, max(other, 0.0),
                               t.get("source") or "manual"))
        return out

    def _compute(self, manual: list[dict]) -> dict:
        cfg, today = self.settings.tax, self.ctx.today()
        rows, held = A.tax_lots(self._fills(), manual, cfg.long_term_months)
        holdings = {(r.get("platform") or "", r["symbol"]): int(r["qty"] or 0)
                    for r in self.reader.query(HOLDINGS_SQL).to_dicts()}
        summary = A.summarize(rows, cfg)
        opens = A.open_lots(held, holdings, today, cfg.long_term_months)
        self.store.replace("tax_lots", rows)
        self.store.replace("tax_summary", summary)
        self.store.replace("tax_open_lots", opens)
        fy = A.fy_of(today)
        tot = next((r for r in summary if r["fy"] == fy and r["category"] == "total"), {})
        return {"fy": fy, "est_tax": tot.get("est_tax", 0.0), "taxable": tot.get("taxable", 0.0),
                "unmatched_sales": sum(r["category"] == "unmatched" for r in rows),
                "holdings_without_date": sum(1 for o in opens if o["buy_date"] is None), "as_of": self.clock.time()}

    async def recompute(self) -> dict:
        if not self.settings.tax.enabled:
            self.headline = {"enabled": False}
            return self.headline
        async with self._lock:
            self.headline = await asyncio.to_thread(self._compute, self._manual())
            return self.headline

    # ---- commands --------------------------------------------------------------------------------------------
    async def cmd_recompute(self, data: dict) -> dict:
        return await self.recompute()

    async def cmd_status(self, data: dict) -> dict:
        return {**self.headline, "history": self._state(), "manual_lots": len(self._manual())}

    async def cmd_backfill(self, data: dict) -> dict:
        r = await self.ensure_history(force=True)
        await self.recompute()
        return {"done": r["ok_history"], "reason": r.get("last_error"), **r}

    async def cmd_lots(self, data: dict) -> dict:
        return {"lots": self._manual()}

    async def cmd_add_lot(self, data: dict) -> dict:
        """``{symbol, qty, buy_date (YYYY-MM-DD), price (per share), fmv_2018?, platform?, note?}``"""
        try:
            symbol = str(data["symbol"]).strip().upper()
            qty, price = int(data["qty"]), float(data["price"])
            buy = date.fromisoformat(str(data["buy_date"]))
            fmv = float(data["fmv_2018"]) if data.get("fmv_2018") not in (None, "", 0, 0.0) else None
        except (KeyError, ValueError, TypeError) as e:
            return {"added": False, "error": f"need symbol, qty, buy_date (YYYY-MM-DD) and price: {e}"}
        if not symbol or qty <= 0 or price <= 0:
            return {"added": False, "error": "symbol, a positive quantity and a positive price are needed"}
        if buy > self.ctx.today():
            return {"added": False, "error": "the purchase date is in the future"}
        if fmv is not None and buy >= A.GRANDFATHER_BEFORE:
            return {"added": False, "error": "the 31 Jan 2018 price only applies to shares bought before 1 Feb 2018"}
        platform = str(data.get("platform") or self._platform_of(symbol))
        cur = self.db.execute("INSERT INTO manual_lots(platform, symbol, qty, buy_date, price, fmv_2018, note, created) "
                              "VALUES (?,?,?,?,?,?,?,?)", (platform, symbol, qty, buy.isoformat(), price, fmv,
                                                           str(data.get("note") or "")[:200], time.time()))
        await self.recompute()
        return {"added": True, "id": cur.lastrowid}

    def _platform_of(self, symbol: str) -> str:
        """Where the stock was traded or is held, when that is one platform; else the current platform."""
        try:
            found = self.reader.query("SELECT DISTINCT platform FROM trades WHERE upper(symbol) = ? UNION "
                                      "SELECT DISTINCT platform FROM holdings WHERE upper(symbol) = ?",
                                      [symbol, symbol])["platform"].to_list()
        except Exception:
            found = []
        found = [x for x in found if x]
        return found[0] if len(found) == 1 else self.settings.platform.name

    async def cmd_delete_lot(self, data: dict) -> dict:
        n = self.db.execute("DELETE FROM manual_lots WHERE id = ?", (int(data["id"]),)).rowcount
        await self.recompute()
        return {"deleted": n}
