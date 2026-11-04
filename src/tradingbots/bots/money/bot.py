"""Money bot: money analysis across platforms and stocks.

Reads the tables bot's ``trades`` / ``ledger`` / ``holdings`` / ``positions`` / ``quotes_latest`` /
``funds`` plus the AI cost log, keeps manual expenses (dashboard form or ``cmd.money.add_expense``)
and writes ``money_allocation``, ``money_pnl``, ``money_charges``, ``money_cashflows``,
``money_expenses`` and ``money_summary``.
"""

from __future__ import annotations

import asyncio
import csv
import sqlite3
import time
from datetime import date

from ...core import topics
from ...core.bot import Bot
from ...core.bus import Message
from ...core.tables_io import TableReader, TableStore
from . import analytics as A

WATCHED = {"trades", "ledger", "holdings", "positions", "ai_costs"}


class MoneyBot(Bot):
    name = "money"

    def configure(self) -> None:
        self.on(topics.TABLES_UPDATED, self.on_tables)
        self.command("add_expense", self.cmd_add_expense)
        self.command("delete_expense", self.cmd_delete_expense)
        self.command("list_expenses", self.cmd_list_expenses)
        self.command("recompute", self.cmd_recompute)
        self.every(60.0, self._periodic, name="periodic", run_at_start=False)
        self._pending = False
        self._last_run = 0.0
        self.notes: list[str] = []
        self.summary: dict = {}

    async def setup(self) -> None:
        self.store = TableStore(self.ctx.paths.tables)
        self.reader = TableReader(self.ctx.paths.tables)
        self.db = sqlite3.connect(self.ctx.paths.bot("money") / "expenses.db", isolation_level=None,
                                  check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, "
                        "category TEXT, platform TEXT, amount REAL, note TEXT, created REAL)")
        self.spawn(self.recompute(), name="startup")

    async def teardown(self) -> None:
        self.db.close()

    def details(self) -> dict:
        return {"summary": self.summary, "notes": self.notes[:5]}

    async def on_tables(self, msg: Message) -> None:
        if msg.data.get("table") in WATCHED and not self._pending:
            self._pending = True
            self.spawn(self._debounced(), name="debounced")

    async def _debounced(self) -> None:
        await asyncio.sleep(2.0)
        self._pending = False
        await self.recompute()

    async def _periodic(self) -> None:
        if time.monotonic() - self._last_run >= self.settings.schedule.money_interval_s:
            await self.recompute()

    # ---- expenses ---------------------------------------------------------------------------------
    async def cmd_add_expense(self, data: dict) -> dict:
        try:
            day = date.fromisoformat(str(data.get("day") or self.ctx.today().isoformat()))
            amount = float(data["amount"])
        except (KeyError, ValueError) as e:
            return {"added": False, "error": f"need day (YYYY-MM-DD) and amount: {e}"}
        if amount <= 0:
            return {"added": False, "error": "amount must be positive"}
        cat = str(data.get("category") or "other")
        cur = self.db.execute("INSERT INTO expenses(day, category, platform, amount, note, created) VALUES (?,?,?,?,?,?)",
                              (day.isoformat(), cat, str(data.get("platform") or ""), amount,
                               str(data.get("note") or ""), time.time()))
        await self.recompute()
        return {"added": True, "id": cur.lastrowid}

    async def cmd_delete_expense(self, data: dict) -> dict:
        n = self.db.execute("DELETE FROM expenses WHERE id=?", (int(data["id"]),)).rowcount
        await self.recompute()
        return {"deleted": n}

    async def cmd_list_expenses(self, data: dict) -> dict:
        return {"expenses": self._manual_expenses()}

    def _manual_expenses(self) -> list[dict]:
        rows = self.db.execute("SELECT id, day, category, platform, amount, note FROM expenses ORDER BY day DESC").fetchall()
        return [{"id": r[0], "day": r[1], "category": r[2], "platform": r[3], "amount": r[4], "note": r[5]} for r in rows]

    # ---- computation ----------------------------------------------------------------------------------
    async def cmd_recompute(self, data: dict) -> dict:
        return await self.recompute()

    async def recompute(self) -> dict:
        self._last_run = time.monotonic()
        res = await asyncio.to_thread(self._compute)
        self.summary = {r["metric"]: r["value"] for r in res["summary"]}
        await self.publish(topics.MONEY_UPDATED, {"as_of": self.clock.time(), "summary": self.summary})
        return {"summary": self.summary, "notes": self.notes}

    def _sectors(self) -> dict[str, str]:
        f = self.settings.money.sector_map_file
        if not f:
            return {}
        p = self.ctx.paths.resolve(f)
        if not p.exists():
            return {}
        with p.open(newline="", encoding="utf-8") as fh:
            return {r["symbol"].strip().upper(): r.get("sector", "").strip() for r in csv.DictReader(fh)}

    def _compute(self) -> dict:
        q = self.reader.query
        today = self.ctx.today()
        trades = q("SELECT *, date AS day FROM trades ORDER BY trade_ts").to_dicts()
        ledger = q("SELECT *, date AS day FROM ledger").to_dicts()
        holdings = q("SELECT * FROM holdings WHERE date = (SELECT MAX(date) FROM holdings)").to_dicts()
        positions = q("SELECT * FROM positions WHERE date = ?", [today]).to_dicts()
        ltp = {r["symbol"]: r["ltp"] for r in q("SELECT symbol, ltp FROM quotes_latest").to_dicts() if r["ltp"]}
        for r in q("SELECT c.symbol, c.close FROM candles_daily c JOIN (SELECT symbol, MAX(day) AS d "
                   "FROM candles_daily GROUP BY symbol) m ON c.symbol = m.symbol AND c.day = m.d").to_dicts():
            ltp.setdefault(r["symbol"], r["close"])
        funds = q("SELECT available FROM funds ORDER BY as_of_ts DESC LIMIT 1")
        funds_available = float(funds["available"][0]) if funds.height else None
        avg_cost = {(h.get("platform") or "", h["symbol"]): float(h["avg_cost"] or 0) for h in holdings}

        realized, _, self.notes = A.fifo_realized(trades, avg_cost)
        alloc = A.allocation(holdings, positions, ltp, self._sectors())
        charges_by_day: dict[tuple, float] = {}
        for t in trades:
            k = (t.get("platform") or "", t["day"], t.get("symbol") or "")
            charges_by_day[k] = charges_by_day.get(k, 0.0) + float(t.get("total_charges") or 0)
        pnl_rows = []
        keys = {(r["platform"], r["day"], r["symbol"]) for r in realized} | set(charges_by_day)
        real_map = {(r["platform"], r["day"], r["symbol"]): r["realized"] for r in realized}
        unreal = {(a["platform"], a["symbol"]): a["pnl"] for a in alloc}
        for k in sorted(keys):
            ch = round(charges_by_day.get(k, 0.0), 2)
            rz = real_map.get(k, 0.0)
            ur = unreal.get((k[0], k[2]), 0.0) if k[1] == today else 0.0
            pnl_rows.append({"platform": k[0], "day": k[1], "symbol": k[2], "realized": rz, "unrealized": ur,
                             "charges": ch, "net": round(rz - ch, 2)})
        charges = A.charges_by_month(trades)
        cash = A.cashflows_by_month(ledger)
        expenses = [{"id": e["id"], "day": date.fromisoformat(e["day"]), "category": e["category"],
                     "platform": e["platform"], "amount": e["amount"], "note": e["note"], "auto": False}
                    for e in self._manual_expenses()]
        ai = q("SELECT date AS day, SUM(inr) AS amount FROM ai_costs GROUP BY date").to_dicts()
        for i, r in enumerate(ai):
            expenses.append({"id": -(i + 1), "day": r["day"], "category": "AI API", "platform": "anthropic",
                             "amount": round(float(r["amount"] or 0), 2), "note": "Claude API usage (auto)", "auto": True})
        summ = A.summary(alloc, pnl_rows, charges, cash, expenses, funds_available, today, ledger)
        self.store.replace("money_allocation", alloc)
        self.store.replace("money_pnl", pnl_rows)
        self.store.replace("money_charges", charges)
        self.store.replace("money_cashflows", cash)
        self.store.replace("money_expenses", expenses)
        self.store.replace("money_summary", summ)
        return {"summary": summ}
