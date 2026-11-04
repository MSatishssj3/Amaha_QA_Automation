"""Budget bot: the daily budget gatekeeper's policy, bookkeeping and alerts.

* Publishes your limits from settings (``budget.policy``, also kept in the key-value store) every
  30 s and immediately when you change them. The execution bot's ``BudgetGuard`` enforces them on
  every entry order, in-process and fail-closed.
* Counts **today's usage** from two sources:
  - the trading engine's own fills (``exec.fill``, entry legs only);
  - trades you place yourself in the Dhan app, read from the broker trade book (``account.trades``).
    These are BUY trades whose order id is not one of the engine's orders. Manual sells are treated
    as exits and never use budget.
* Publishes ``budget.usage`` (each limit with used, remaining and % used, for the dashboard) and
  ``budget.external`` (your own trades, which the guard adds to its own count).
* Raises an alert once per day per limit and threshold (``alert_at_pct``, default 80 % and 100 %),
  and once per day for the first entry the budget blocks under each rule.
* Writes an end-of-day summary (``cmd.budget.history``).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta

import orjson
from dhanbot.clock import ist_datetime

from ...core import topics
from ...core.bot import Bot
from ...core.bus import Message
from ...core.settings import Settings, diff

MAIN_LEG = "MAIN"


def _inr(x: float) -> str:
    return f"₹{x:,.0f}"


class BudgetBot(Bot):
    name = "budget"
    start_from = "earliest"  # a first start replays the retained events, so today's usage is complete

    def configure(self) -> None:
        self.on(topics.EXEC_FILL, self.on_fill)
        self.on(topics.EXEC_ORDER, self.on_order)
        self.on(topics.EXEC_PNL, self.on_pnl)
        self.on(topics.EXEC_REJECT, self.on_reject)
        self.on(topics.ACCOUNT_TRADES, self.on_trades)
        self.on(topics.ACCOUNT_ORDERS, self.on_orders)
        self.command("status", self.cmd_status)
        self.command("history", self.cmd_history)
        self.every(30.0, self.publish_policy, name="policy", run_at_start=True)
        self.every(2.0, self.flush, name="usage", run_at_start=True)
        self.daily(lambda: self.settings.schedule.budget_summary, self.end_of_day, name="summary")
        self.day = ""
        self.trades_today: list[dict] = []
        self.order_cids: dict[str, str] = {}  # broker order id -> correlation id, from today's order book
        self.pnl: dict = {}
        self.usage: dict = {}
        self.external: dict = {}
        self._dirty = True
        self._last_usage_pub = 0.0

    def details(self) -> dict:
        lim = {i["rule"]: round(i["pct"], 1) for i in self.usage.get("limits", [])[:6]}
        return {"date": self.usage.get("date"), "pct_used": lim, "budget_rejects": self.usage.get("budget_rejects")}

    async def setup(self) -> None:
        self.db = sqlite3.connect(self.ctx.paths.bot("budget") / "budget.db", isolation_level=None,
                                  check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        for ddl in (
            "CREATE TABLE IF NOT EXISTS fills (day TEXT, key TEXT PRIMARY KEY, cid TEXT, symbol TEXT, side TEXT, "
            "qty INTEGER, price REAL, value REAL, intent TEXT, leg TEXT, ts REAL)",
            "CREATE TABLE IF NOT EXISTS bot_orders (day TEXT, broker_id TEXT, cid TEXT, PRIMARY KEY (day, broker_id))",
            "CREATE TABLE IF NOT EXISTS alerts (day TEXT, key TEXT, ts REAL, PRIMARY KEY (day, key))",
            "CREATE TABLE IF NOT EXISTS rejects (day TEXT, rule TEXT, count INTEGER, PRIMARY KEY (day, rule))",
            "CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, summary TEXT)",
        ):
            self.db.execute(ddl)
        cutoff = (self.ctx.today() - timedelta(days=400)).isoformat()
        for t in ("fills", "bot_orders", "alerts", "rejects"):
            self.db.execute(f"DELETE FROM {t} WHERE day < ?", (cutoff,))
        self.day = self.ctx.today().isoformat()

    async def teardown(self) -> None:
        self.db.close()

    async def on_settings(self, old: Settings, new: Settings) -> None:
        if old.budget != new.budget or old.platform.name != new.platform.name:
            await self.publish_policy()
            self._dirty = True
            changes = diff({"budget": old.budget.model_dump()}, {"budget": new.budget.model_dump()})
            if changes:
                await self.alert("daily budget updated: " + "; ".join(changes), "INFO", key="budget-changed")

    # ---- policy -------------------------------------------------------------------------------------------
    def policy(self) -> dict:
        b = self.settings.budget
        return {"version": self.settings_version, "ts": self.clock.time(), "date": self.ctx.today().isoformat(),
                "platform": self.settings.platform.name, "enabled": b.enabled, "limits": b.model_dump()}

    async def publish_policy(self) -> None:
        p = self.policy()
        await self.bus.kv_set(topics.KV_BUDGET_POLICY, p)
        await self.publish(topics.BUDGET_POLICY, p)

    # ---- inputs ---------------------------------------------------------------------------------------------
    def _today(self) -> str:
        return self.ctx.today().isoformat()

    @staticmethod
    def _day_of(ts: float | None) -> str:
        return ist_datetime(float(ts)).date().isoformat() if ts else ""

    async def on_fill(self, msg: Message) -> None:
        f = msg.data
        day = self._day_of(f.get("ts") or msg.ts)
        key = f"{f.get('cid')}|{f.get('leg')}|{f.get('ts')}|{f.get('qty')}|{f.get('price')}"
        value = float(f.get("value") or (f.get("qty") or 0) * (f.get("price") or 0))
        self.db.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (day, key, f.get("cid"), f.get("symbol"), f.get("side"), f.get("qty"), f.get("price"), value,
                         f.get("intent"), f.get("leg") or MAIN_LEG, f.get("ts")))
        self._dirty = True

    async def on_order(self, msg: Message) -> None:
        o = msg.data
        if o.get("broker_id"):
            self.db.execute("INSERT OR IGNORE INTO bot_orders VALUES (?,?,?)",
                            (self._day_of(o.get("updated_ts") or msg.ts), o["broker_id"], o.get("cid") or ""))
            self._dirty = True

    async def on_pnl(self, msg: Message) -> None:
        if msg.data.get("date") == self._today():
            self.pnl = msg.data
            self._dirty = True

    async def on_reject(self, msg: Message) -> None:
        r = msg.data
        day = self._day_of(r.get("ts") or msg.ts)
        rule = str(r.get("rule") or "rejected")
        self.db.execute("INSERT INTO rejects VALUES (?,?,?) "
                        "ON CONFLICT(day, rule) DO UPDATE SET count = count + excluded.count",
                        (day, rule, int(r.get("count") or 1)))
        self._dirty = True
        if r.get("budget") and day == self._today():
            await self._alert_once(f"reject:{rule}", f"daily budget blocked an entry ({r.get('symbol')}): "
                                                     f"{r.get('reason')}", "WARNING")

    async def on_trades(self, msg: Message) -> None:
        d = msg.data
        if d.get("kind") == "trades" and d.get("date") == self._today():
            self.trades_today = list(d.get("rows") or [])
            self._dirty = True

    async def on_orders(self, msg: Message) -> None:
        d = msg.data
        if d.get("date") == self._today():
            self.order_cids = {str(r["order_id"]): str(r.get("correlation_id") or "") for r in d.get("rows") or []
                               if r.get("order_id")}
            self._dirty = True

    # ---- usage ----------------------------------------------------------------------------------------------
    def compute(self, cid_prefix: str = "DB", exec_status: dict | None = None) -> tuple[dict, dict]:
        today, now = self._today(), self.clock.time()
        s, b = self.settings, self.settings.budget
        bot_turn, bot_stock = 0.0, defaultdict(float)
        bot_cids: set[str] = set()
        bot_cids_stock: dict[str, set[str]] = defaultdict(set)
        for cid, sym, value, leg in self.db.execute(
                "SELECT cid, symbol, value, leg FROM fills WHERE day = ? AND intent = 'ENTRY'", (today,)):
            if (leg or MAIN_LEG) != MAIN_LEG:
                continue  # super-order target / stop legs are exits
            bot_turn += value or 0.0
            bot_stock[sym] += value or 0.0
            bot_cids.add(cid)
            bot_cids_stock[sym].add(cid)
        bot_ids = {r[0] for r in self.db.execute("SELECT broker_id FROM bot_orders WHERE day = ?", (today,))}
        if cid_prefix:
            bot_ids |= {oid for oid, cid in self.order_cids.items() if cid.startswith(cid_prefix)}
        ext_turn, ext_stock = 0.0, defaultdict(float)
        ext_orders: set[str] = set()
        ext_orders_stock: dict[str, set[str]] = defaultdict(set)
        for t in self.trades_today:
            oid = str(t.get("order_id") or "")
            if oid in bot_ids or str(t.get("side") or "").upper() != "BUY":
                continue
            v = float(t.get("value") or float(t.get("qty") or 0) * float(t.get("price") or 0))
            sym = str(t.get("symbol") or "?")
            ext_turn += v
            ext_stock[sym] += v
            ext_orders.add(oid or f"{sym}-{t.get('trade_ts')}")
            ext_orders_stock[sym].add(oid or f"{sym}-{t.get('trade_ts')}")
        external = {"date": today, "ts": now, "platform": s.platform.name, "turnover_inr": round(ext_turn, 2),
                    "trades": len(ext_orders), "per_stock": {k: round(v, 2) for k, v in ext_stock.items()},
                    "trades_by_stock": {k: len(v) for k, v in ext_orders_stock.items()}}
        count_ext = b.count_manual_trades
        ext_t = ext_turn if count_ext else 0.0
        ext_n = len(ext_orders) if count_ext else 0
        exposure = float(self.pnl.get("exposure") or 0.0)
        net = float(self.pnl.get("net") or 0.0)
        items: list[dict] = []

        def item(rule: str, label: str, used: float, limit: float, unit: str = "inr") -> None:
            if limit and limit > 0:
                items.append({"rule": rule, "label": label, "unit": unit, "used": round(used, 2), "limit": limit,
                              "remaining": round(max(0.0, limit - used), 2), "pct": round(used / limit * 100, 2)})

        item("daily_capital", "New entries today", bot_turn + ext_t, b.daily_capital_limit_inr)
        item("capital_in_use", "Capital in use now (engine)", exposure, b.max_capital_in_use_inr)
        item("trades_per_day", "Entries today", len(bot_cids) + ext_n, b.max_trades_per_day, "count")
        item("daily_loss", "Loss today (engine, net of charges)", max(0.0, -net), b.max_daily_loss_inr)
        plat_lim = b.per_platform_daily_limit_inr.get(s.platform.name, 0.0)
        item(f"platform:{s.platform.name}", f"Entries on {s.platform.name}", bot_turn + ext_t, plat_lim)
        symbols = sorted(set(bot_stock) | (set(ext_stock) if count_ext else set()) | set(b.per_stock_overrides))
        for sym in symbols:
            used = bot_stock.get(sym, 0.0) + (ext_stock.get(sym, 0.0) if count_ext else 0.0)
            item(f"stock:{sym}", f"{sym} entries today", used, b.per_stock_overrides.get(sym, b.per_stock_daily_limit_inr))
            n = len(bot_cids_stock.get(sym, ())) + (len(ext_orders_stock.get(sym, ())) if count_ext else 0)
            item(f"stock_trades:{sym}", f"{sym} entries (count)", n, b.max_trades_per_stock, "count")
        rejects = dict(self.db.execute("SELECT rule, count FROM rejects WHERE day = ?", (today,)).fetchall())
        st = exec_status or {}
        usage = {
            "date": today, "ts": now, "enabled": b.enabled, "policy_version": self.settings_version,
            "platform": s.platform.name, "count_manual_trades": count_ext,
            "bot": {"turnover_inr": round(bot_turn, 2), "entries": len(bot_cids),
                    "per_stock": {k: round(v, 2) for k, v in bot_stock.items()},
                    "entries_by_stock": {k: len(v) for k, v in bot_cids_stock.items()}},
            "external": {k: external[k] for k in ("turnover_inr", "trades", "per_stock", "trades_by_stock")},
            "total": {"turnover_inr": round(bot_turn + ext_t, 2), "entries": len(bot_cids) + ext_n},
            "exposure_inr": round(exposure, 2), "net_pnl_inr": round(net, 2), "limits": items, "rejects": rejects,
            "budget_rejects": sum(v for k, v in rejects.items() if k.startswith("budget")),
            "engine": {"running": bool(st.get("running")), "mode": st.get("mode"), "state": st.get("state"),
                       "note": st.get("note")},
        }
        return usage, external

    async def flush(self) -> None:
        today = self._today()
        if today != self.day:  # new trading day: yesterday's caches no longer apply
            self.day, self.trades_today, self.order_cids, self.pnl = today, [], {}, {}
            self._dirty = True
        now = self.clock.time()
        if not self._dirty and now - self._last_usage_pub < 60:
            return
        self._dirty = False
        st = await self.bus.kv_get(topics.KV_EXEC_STATUS) or {}
        usage, external = self.compute(str(st.get("cid_prefix") or "DB"), st)
        if _strip_ts(external) != _strip_ts(self.external):
            self.external = external
            await self.bus.kv_set(topics.KV_BUDGET_EXTERNAL, external)
            await self.publish(topics.BUDGET_EXTERNAL, external)
        if _strip_ts(usage) != _strip_ts(self.usage) or now - self._last_usage_pub >= 60:
            self.usage, self._last_usage_pub = usage, now
            await self.bus.kv_set(topics.KV_BUDGET_USAGE, usage)
            await self.publish(topics.BUDGET_USAGE, usage)
        await self._threshold_alerts(usage)

    async def _threshold_alerts(self, usage: dict) -> None:
        b = self.settings.budget
        if not b.enabled:
            return
        for it in usage["limits"]:
            for t in b.alert_at_pct:
                if it["pct"] + 1e-9 < t:
                    break
                if it["unit"] == "count":
                    amount = f"{it['used']:.0f} of {it['limit']:.0f}"
                else:
                    amount = f"{_inr(it['used'])} of {_inr(it['limit'])} ({_inr(it['remaining'])} left)"
                if t >= 100:
                    effect = ("trading is halted for the day" if it["rule"] == "daily_loss"
                              else "new entries are blocked")
                    text = f"daily budget: {it['label']} limit reached - {amount}; {effect}"
                else:
                    text = f"daily budget: {it['label']} at {it['pct']:.0f}% - {amount}"
                await self._alert_once(f"{it['rule']}@{t:g}", text, "ERROR" if t >= 100 else "WARNING")

    async def _alert_once(self, key: str, text: str, level: str) -> None:
        cur = self.db.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?)", (self._today(), key, self.clock.time()))
        if cur.rowcount == 1:
            await self.alert(text, level, key=f"budget:{key}")

    # ---- end of day -------------------------------------------------------------------------------------------
    async def end_of_day(self) -> None:
        st = await self.bus.kv_get(topics.KV_EXEC_STATUS) or {}
        usage, _ = self.compute(str(st.get("cid_prefix") or "DB"), st)
        self.db.execute("INSERT OR REPLACE INTO days VALUES (?, ?)", (usage["date"], orjson.dumps(usage).decode()))
        b = self.settings.budget
        lim = f" of {_inr(b.daily_capital_limit_inr)}" if b.daily_capital_limit_inr else ""
        await self.alert(f"budget summary {usage['date']}: entries {_inr(usage['total']['turnover_inr'])}{lim} "
                         f"(engine {_inr(usage['bot']['turnover_inr'])}, yours {_inr(usage['external']['turnover_inr'])}); "
                         f"{usage['total']['entries']} entries; {usage['budget_rejects']} blocked by the budget; "
                         f"engine P&L {_inr(usage['net_pnl_inr'])}", "INFO", key="budget-summary")

    # ---- commands ---------------------------------------------------------------------------------------------
    async def cmd_status(self, data: dict) -> dict:
        st = await self.bus.kv_get(topics.KV_EXEC_STATUS) or {}
        usage, external = self.compute(str(st.get("cid_prefix") or "DB"), st)
        return {"policy": self.policy(), "usage": usage, "external": external}

    async def cmd_history(self, data: dict) -> dict:
        days = max(1, min(int(data.get("days") or 30), 400))
        since = (date.fromisoformat(self._today()) - timedelta(days=days)).isoformat()
        rows = self.db.execute("SELECT day, summary FROM days WHERE day >= ? ORDER BY day DESC", (since,)).fetchall()
        alerts = self.db.execute("SELECT day, key, ts FROM alerts WHERE day >= ? ORDER BY ts DESC LIMIT 200",
                                 (since,)).fetchall()
        return {"days": [orjson.loads(s) for _, s in rows],
                "alerts": [{"day": d, "key": k, "ts": t} for d, k, t in alerts]}


def _strip_ts(d: dict) -> dict:
    return {k: v for k, v in (d or {}).items() if k != "ts"}
