"""Journal bot: how your trading is actually going.

* Builds **round trips** (flat to flat) from two sources:
  - the broker's trade book: engine and manual trades, with their actual charges;
  - the engine's paper fills, kept in a separate book so paper results never mix with real money.
* For engine orders it adds slippage against the order's reference price, and how the trade ended: target,
  stop-loss, square-off, strategy exit and so on.
* Writes three tables:
  - ``perf_trades``: every round trip;
  - ``perf_summary``: win rate, expectancy, profit factor, drawdown, streaks, charges and slippage, by period
    and source;
  - ``perf_breakdown``: by hour, weekday, symbol, holding time, direction, source, exit and tag.
* Your tags and notes per trade live in ``data/journal/journal.db`` (``cmd.journal.annotate``). Trade keys are
  stable, so notes survive every recompute.
* It recomputes a few seconds after the trade tables change, and just after midnight, when the "today" figures
  roll over.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import Counter, defaultdict

from ...core import topics
from ...core.bot import Bot
from ...core.bus import Message
from ...core.tables_io import TableReader, TableStore
from . import analytics as A

WATCHED = {"trades", "exec_orders", "exec_fills", "holdings"}
LEG_ROLE = {"TARGET_LEG": "target", "STOP_LOSS_LEG": "sl"}
CHARGE_COLS = ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "gst")
HOLDING_COST_SQL = ("SELECT h.platform, h.symbol, h.avg_cost FROM holdings h JOIN (SELECT platform, symbol, MAX(date) AS d "
                    "FROM holdings GROUP BY platform, symbol) m ON h.platform IS NOT DISTINCT FROM m.platform "
                    "AND h.symbol = m.symbol AND h.date = m.d")


def _side(v) -> int:
    s = str(v or "").upper()
    return 1 if s == "BUY" else -1 if s == "SELL" else 0


def _charges(t: dict) -> float:
    if t.get("total_charges") is not None:
        return float(t["total_charges"])
    return sum(float(t.get(c) or 0) for c in CHARGE_COLS)


class JournalBot(Bot):
    name = "journal"

    def configure(self) -> None:
        self.on(topics.TABLES_UPDATED, self.on_tables)
        self.command("annotate", self.cmd_annotate)
        self.command("notes", self.cmd_notes)
        self.command("recompute", self.cmd_recompute)
        self.command("stats", self.cmd_stats)
        self.daily("00:01", self.recompute, name="new-day", trading_days_only=False)
        self.trips: list[dict] = []
        self.headline: dict = {}
        self._pending = False
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        self.store = TableStore(self.ctx.paths.tables)
        self.reader = TableReader(self.ctx.paths.tables)
        self.db = sqlite3.connect(self.ctx.paths.bot("journal") / "journal.db", isolation_level=None,
                                  check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS notes (trade_key TEXT PRIMARY KEY, tags TEXT NOT NULL DEFAULT '', "
                        "note TEXT NOT NULL DEFAULT '', updated REAL)")
        self.spawn(self.recompute(), name="startup")

    async def teardown(self) -> None:
        self.db.close()

    def details(self) -> dict:
        h = self.headline
        today = h.get("today") or {}
        return {"closed": (h.get("all") or {}).get("trades", 0), "open": h.get("open", 0),
                "unmatched": h.get("unmatched", 0),
                "today": {k: today.get(k) for k in ("trades", "win_rate", "net_pnl")}}

    async def on_tables(self, msg: Message) -> None:
        if msg.data.get("table") in WATCHED and not self._pending:
            self._pending = True
            self.spawn(self._debounced(), name="debounced")

    async def _debounced(self) -> None:
        await asyncio.sleep(3.0)
        self._pending = False
        await self.recompute()

    # ---- inputs -------------------------------------------------------------------------------------------
    def _load(self) -> tuple[list[A.Fill], dict[tuple[str, str], float]]:
        q = self.reader.query
        orders = q("SELECT * FROM exec_orders").to_dicts()
        by_broker = {o["broker_id"]: o for o in orders if o.get("broker_id")}
        by_cid = {o["cid"]: o for o in orders if o.get("cid")}
        efills = q("SELECT * FROM exec_fills").to_dicts()
        legs: dict[tuple[str, str], set[str]] = defaultdict(set)
        for f in efills:
            if f.get("broker_id"):
                legs[(f["broker_id"], str(f.get("side") or "").upper())].add(f.get("leg") or "")
        fills: list[A.Fill] = []
        seen: set[tuple[str, str]] = set()
        for t in q("SELECT * FROM trades").to_dicts():
            tid = str(t.get("trade_id") or f"{t.get('order_id')}@{t.get('trade_ts')}")
            k = (t.get("platform") or "", tid)
            if k in seen:
                continue
            seen.add(k)
            o = by_broker.get(t.get("order_id") or "")
            role, ref = "", None
            if o is not None:
                if str(o.get("side") or "").upper() == str(t.get("side") or "").upper():
                    role, ref = o.get("role") or "", o.get("ref_price")
                else:  # the opposite side of an engine super order: one of its exit legs
                    lg = legs.get((o["broker_id"], str(t.get("side") or "").upper()), set())
                    role = LEG_ROLE.get(next(iter(lg)), "bracket") if len(lg) == 1 else "bracket"
            source = "bot" if o is not None or t.get("source") == "bot" else "manual"
            fills.append(A.Fill("real", k[0], t.get("symbol") or "", t.get("product") or "", tid,
                                float(t.get("trade_ts") or 0), _side(t.get("side")), int(t.get("qty") or 0),
                                float(t.get("price") or 0), _charges(t), source, t.get("segment") or "", role, ref))
        n_by_id: Counter = Counter()
        paper = sorted((f for f in efills if f.get("mode") == "paper"),
                       key=lambda f: (float(f.get("ts") or 0), f.get("cid") or "", f.get("qty") or 0, f.get("price") or 0))
        for f in paper:
            base = f"{f.get('cid')}@{float(f.get('ts') or 0):.3f}"
            n_by_id[base] += 1
            fid = base if n_by_id[base] == 1 else f"{base}#{n_by_id[base]}"
            o = by_cid.get(f.get("cid") or "")
            leg = f.get("leg") or ""
            same = o is not None and str(o.get("side") or "").upper() == str(f.get("side") or "").upper()
            role = LEG_ROLE.get(leg) or (f.get("role") or "")
            ref = o.get("ref_price") if same and leg in ("", "ENTRY_LEG") else None
            fills.append(A.Fill("paper", f.get("platform") or "", f.get("symbol") or "", f.get("product") or "", fid,
                                float(f.get("ts") or 0), _side(f.get("side")), int(f.get("qty") or 0),
                                float(f.get("price") or 0), float(f.get("est_charges") or 0), "paper",
                                f.get("segment") or "", role, ref))
        cost = {(h.get("platform") or "", h["symbol"]): float(h["avg_cost"])
                for h in q(HOLDING_COST_SQL).to_dicts() if h.get("avg_cost")}
        return fills, cost

    def _notes(self) -> dict[str, tuple[str, str]]:
        return {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT trade_key, tags, note FROM notes")}

    # ---- computation -----------------------------------------------------------------------------------------
    def _compute(self, notes: dict[str, tuple[str, str]]) -> tuple[list[dict], dict]:
        fills, cost = self._load()
        trips = A.round_trips(fills, cost)
        for t in trips:
            if t["trade_key"] in notes:
                t["tags"], t["note"] = notes[t["trade_key"]]
        today = self.ctx.today()
        self.store.replace("perf_trades", trips)
        self.store.replace("perf_summary", A.summary_rows(trips, today))
        self.store.replace("perf_breakdown", A.breakdown_rows(trips, today))
        real = A.of_source(trips, "real")
        headline = {"as_of": self.clock.time(), "day": today.isoformat(),
                    "today": A.stats(t for t in real if A.in_scope(t, "today", today)),
                    "month": A.stats(t for t in real if A.in_scope(t, "month", today)),
                    "all": A.stats(real), "open": sum(t["status"] == "open" for t in real),
                    "unmatched": sum(t["status"] == "unmatched" for t in real),
                    "paper_today": A.stats(t for t in A.of_source(trips, "paper") if A.in_scope(t, "today", today))}
        return trips, headline

    async def recompute(self) -> dict:
        async with self._lock:
            t0 = time.monotonic()
            self.trips, self.headline = await asyncio.to_thread(self._compute, self._notes())
            await self.publish(topics.JOURNAL_UPDATED, self.headline)
            self.log.debug("journal: %d round trips in %.2fs", len(self.trips), time.monotonic() - t0)
            return self.headline

    # ---- commands -------------------------------------------------------------------------------------------
    async def cmd_recompute(self, data: dict) -> dict:
        return await self.recompute()

    async def cmd_annotate(self, data: dict) -> dict:
        """``{trade_key, tags, note}``, or ``{items: [...]}`` for several. Empty tags and note remove the entry."""
        items = data.get("items") if isinstance(data.get("items"), list) else [data]
        known = {t["trade_key"] for t in self.trips}
        bad = [str(i.get("trade_key")) for i in items if str(i.get("trade_key") or "") not in known]
        if bad:
            return {"saved": False, "error": f"not in the journal: {', '.join(bad[:5])}"}
        now = time.time()
        for i in items:
            tags = ",".join(A.parse_tags(i.get("tags")))
            note = str(i.get("note") or "").strip()[:2000]
            if tags or note:
                self.db.execute("INSERT INTO notes(trade_key, tags, note, updated) VALUES (?,?,?,?) ON CONFLICT(trade_key) "
                                "DO UPDATE SET tags=excluded.tags, note=excluded.note, updated=excluded.updated",
                                (i["trade_key"], tags, note, now))
            else:
                self.db.execute("DELETE FROM notes WHERE trade_key=?", (i["trade_key"],))
        await self.recompute()
        return {"saved": True, "count": len(items)}

    async def cmd_notes(self, data: dict) -> dict:
        rows = self.db.execute("SELECT trade_key, tags, note, updated FROM notes ORDER BY updated DESC").fetchall()
        return {"notes": [{"trade_key": r[0], "tags": [x for x in r[1].split(",") if x], "note": r[2], "updated": r[3]}
                          for r in rows]}

    async def cmd_stats(self, data: dict) -> dict:
        scope, source = str(data.get("scope") or "all"), str(data.get("source") or "real")
        if scope not in A.SCOPES or source not in A.SOURCES:
            return {"ok": False, "error": f"scope is one of {A.SCOPES}, source one of {A.SOURCES}"}
        today = self.ctx.today()
        ts = [t for t in A.of_source(self.trips, source) if A.in_scope(t, scope, today)]
        return {"scope": scope, "source": source, "stats": A.stats(ts)}
