"""Seed a project folder with tables, key-value state and events that look like a running team, plus a
fake responder that answers the bots' commands, so dashboard pages can be tested without the bots."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from tradingbots.core import topics
from tradingbots.core.bus import make_bus
from tradingbots.core.settings import parse_settings, write_settings_file
from tradingbots.core.tables_io import TableStore
from tradingbots.dashboard.auth import hash_password
from tradingbots.supervisor import KV_SUPERVISOR

PASSWORD = "correct horse battery staple"
SYMS = ["RELIANCE", "HDFCBANK", "INFY"]


def weekdays(n: int, end: date) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def usage_sample(today: str) -> dict:
    return {"date": today, "ts": time.time(), "enabled": True, "policy_version": 3, "platform": "simulated",
            "count_manual_trades": True,
            "bot": {"turnover_inr": 41200.0, "entries": 4, "per_stock": {"RELIANCE": 29000.0, "INFY": 12200.0},
                    "entries_by_stock": {"RELIANCE": 2, "INFY": 2}},
            "external": {"turnover_inr": 9800.0, "trades": 1, "per_stock": {"HDFCBANK": 9800.0},
                         "trades_by_stock": {"HDFCBANK": 1}},
            "total": {"turnover_inr": 51000.0, "entries": 5}, "exposure_inr": 14500.0, "net_pnl_inr": -320.5,
            "limits": [
                {"rule": "daily_capital", "label": "New entries today", "unit": "inr", "used": 51000.0,
                 "limit": 60000.0, "remaining": 9000.0, "pct": 85.0},
                {"rule": "capital_in_use", "label": "Capital in use now (engine)", "unit": "inr", "used": 14500.0,
                 "limit": 150000.0, "remaining": 135500.0, "pct": 9.67},
                {"rule": "trades_per_day", "label": "Entries today", "unit": "count", "used": 5, "limit": 5,
                 "remaining": 0, "pct": 100.0},
                {"rule": "daily_loss", "label": "Loss today (engine, net of charges)", "unit": "inr", "used": 320.5,
                 "limit": 2000.0, "remaining": 1679.5, "pct": 16.0}],
            "rejects": {"budget_daily_capital": 3, "stale_feed": 1}, "budget_rejects": 3,
            "engine": {"running": True, "mode": "simulated-exchange", "state": "RUNNING", "note": ""}}


def seed(base: Path, today: date) -> None:
    s = parse_settings({"platform": {"name": "simulated"}, "budget": {"daily_capital_limit_inr": 60000,
                                                                       "max_trades_per_day": 5}})
    write_settings_file(base / "config" / "settings.toml", s)
    (base / ".env").write_text(f"DASHBOARD_PASSWORD_HASH={hash_password(PASSWORD)}\n", encoding="utf-8")
    st = TableStore(base / "data" / "tables")
    days = weekdays(320, today - timedelta(days=1))
    candles, metrics, monthly = [], [], []
    for k, sym in enumerate(SYMS):
        px = 1400.0 + 200 * k
        for i, d in enumerate(days):
            o = px
            px = px * (1 + 0.004 * math.sin(i / 9 + k) + 0.0008)
            candles.append({"symbol": sym, "segment": "NSE_EQ", "security_id": 100 + k, "day": d, "open": o,
                            "high": max(o, px) * 1.006, "low": min(o, px) * 0.994, "close": px,
                            "volume": 100000 + 5000 * (i % 7)})
        metrics.append({"symbol": sym, "as_of": days[-1], "close": px, "ret_1d": 0.4 - k * 0.3, "ret_1m": 3.1 - k * 2,
                        "ret_1y": 18.0 - k * 9, "rsi14": 61.0 - 9 * k, "dist_sma50": 2.0 - 1.5 * k,
                        "dist_sma200": 9.0 - 4 * k, "vol_ann": 21.0 + k, "from_high_52w": 3.0 + 4 * k, "adx14": 28.0,
                        "trend": ["strong_up", "sideways", "down"][k], "trend_score": [72.0, 5.0, -40.0][k],
                        "days": len(days)})
        for y in (today.year - 1, today.year):
            for mth in range(1, 13):
                if date(y, mth, 1) < today:
                    monthly.append({"symbol": sym, "year": y, "month": mth, "ret": 4 * math.sin(mth + k + y)})
    for sym in SYMS:
        st.replace("candles_daily", [c for c in candles if c["symbol"] == sym], sym)
    st.replace("history_metrics", metrics)
    st.replace("monthly_returns", monthly)
    st.replace("correlations", [{"symbol_a": a, "symbol_b": b, "corr": 0.62 - 0.3 * i}
                                for i, (a, b) in enumerate([("RELIANCE", "HDFCBANK"), ("RELIANCE", "INFY"),
                                                            ("HDFCBANK", "INFY")])])
    st.replace("quotes_latest", [{"symbol": s, "segment": "NSE_EQ", "ltp": 1500.0 + 100 * i, "change_pct": 0.4 - 0.5 * i,
                                  "updated_ts": time.time()} for i, s in enumerate(SYMS)])
    st.append("bars_1m", [{"symbol": "HDFCBANK", "segment": "NSE_EQ", "security_id": 101, "tf": "1m",
                           "start_ts": time.time() - 60 * (90 - i), "open": 1600.0, "high": 1602.0, "low": 1598.0,
                           "close": 1600 + 3 * math.sin(i / 6), "volume": 1000} for i in range(90)], today)
    st.replace("money_summary", [{"metric": "net_worth", "value": 263450.0, "unit": "INR", "note": "holdings + cash"},
                                 {"metric": "invested", "value": 24250.0, "unit": "INR", "note": ""},
                                 {"metric": "unrealized_pnl", "value": 1510.0, "unit": "INR", "note": ""},
                                 {"metric": "charges_mtd", "value": 212.4, "unit": "INR", "note": ""},
                                 {"metric": "charges_ytd", "value": 2140.0, "unit": "INR", "note": ""},
                                 {"metric": "xirr", "value": 11.8, "unit": "%", "note": ""}])
    st.replace("money_allocation", [{"platform": "simulated", "symbol": "INFY", "kind": "holding", "qty": 10,
                                     "invested": 14800.0, "current_value": 15900.0, "pnl": 1100.0, "weight_pct": 61.2},
                                    {"platform": "simulated", "symbol": "HDFCBANK", "kind": "holding", "qty": 5,
                                     "invested": 9450.0, "current_value": 9860.0, "pnl": 410.0, "weight_pct": 38.8}])
    st.replace("money_pnl", [{"platform": "simulated", "day": d, "symbol": "RELIANCE", "realized": 120.0 * math.sin(i),
                              "unrealized": 0.0, "charges": 18.0, "net": 120.0 * math.sin(i) - 18}
                             for i, d in enumerate(days[-20:])])
    st.replace("money_charges", [{"platform": "simulated", "month": f"{today.year}-{m:02d}", "symbol": "RELIANCE",
                                  "brokerage": 40.0 * m, "stt": 12.0 * m, "exchange_txn": 3.0 * m, "sebi": 0.1 * m,
                                  "stamp_duty": 2.0 * m, "gst": 7.2 * m, "total": 64.3 * m, "turnover": 90000.0 * m,
                                  "pct_of_turnover": 0.07} for m in range(1, today.month + 1)])
    st.replace("money_cashflows", [{"platform": "simulated", "month": f"{today:%Y-%m}", "deposits": 250000.0,
                                    "withdrawals": 0.0, "net": 250000.0}])
    st.replace("money_expenses", [{"id": 1, "day": today, "category": "data plan", "platform": "dhan", "amount": 499.0,
                                   "note": "Dhan data API", "auto": False}])
    st.replace("exec_orders", [{"cid": "DB1", "broker_id": "52001", "symbol": "RELIANCE", "segment": "NSE_EQ",
                                "side": "BUY", "qty": 2, "price": 1450.1, "status": "FILLED", "intent": "ENTRY",
                                "role": "entry", "filled_qty": 2, "avg_price": 1450.0, "reason": "",
                                "updated_ts": time.time()}], today)
    st.append("exec_fills", [{"cid": "DB1", "broker_id": "52001", "symbol": "RELIANCE", "segment": "NSE_EQ",
                              "side": "BUY", "qty": 2, "price": 1450.0, "intent": "ENTRY", "ts": time.time()}], today)
    st.append("ai_costs", [{"ts": time.time(), "model": "claude-opus-5-5", "purpose": "report:midday",
                            "input_tokens": 9000, "output_tokens": 4000, "cache_read_tokens": 2000,
                            "cache_write_tokens": 0, "usd": 0.1164, "inr": 9.89}], today)
    seed_journal(st, today, days)

    async def kv() -> None:
        bus = make_bus("sqlite", "seed", data_dir=base / "data")
        await bus.connect()
        await bus.kv_set(topics.KV_CONFIG, {"version": 3, "settings": s.to_dict()})
        await bus.kv_set(topics.KV_QUOTES, {f"NSE_EQ:{sym}": {"symbol": sym, "segment": "NSE_EQ", "ltp": 1500.0 + 100 * i,
                                                              "bid": 1499.9 + 100 * i, "ask": 1500.1 + 100 * i,
                                                              "volume": 120000, "prev_close": 1494.0 + 100 * i,
                                                              "change_pct": 0.4 - 0.5 * i, "updated_ts": time.time()}
                                            for i, sym in enumerate(SYMS)})
        await bus.kv_set(topics.KV_BUDGET_USAGE, usage_sample(today.isoformat()))
        await bus.kv_set(topics.KV_EXEC_STATUS, {"running": True, "mode": "simulated-exchange", "state": "RUNNING",
                                                 "reason": "", "platform": "simulated", "net_pnl": -320.5,
                                                 "charges": 58.2, "open_positions": 1, "working_orders": 0,
                                                 "guard": {"turnover_inr": 41200.0, "reserved_inr": 0.0,
                                                           "policy_problem": ""}, "cid_prefix": "DB"})
        for name in ("config", "market_data", "tables", "history", "money", "budget", "ai_analyst", "execution",
                     "notifier"):
            await bus.kv_set(topics.kv_heartbeat(name), {"bot": name, "status": "ok", "ts": time.time(),
                                                         "counters": {"errors": 0}, "last_error": "",
                                                         "details": {"spent_today_usd": 0.1164, "budget_usd": 1.0,
                                                                     "reason": "claude-opus-5-5, effort high"}
                                                         if name == "ai_analyst" else {}})
        await bus.kv_set(KV_SUPERVISOR, {"ts": time.time(), "sim": None, "bots": []})
        await bus.publish(topics.ALERT, {"level": "WARNING", "source": "budget", "key": "budget:daily_capital@80",
                                         "text": "daily budget: New entries today at 85%", "ts": time.time()})
        await bus.publish(topics.MARKET_STATUS, {"platform": "simulated", "connected": True, "instruments": 4,
                                                 "frames": 1234, "last_tick_age_s": 0.4})
        await bus.publish(topics.AI_REPORT, {"report_id": "r1", "kind": "midday", "status": "ok",
                                             "headline": "Large caps steady; RELIANCE leads", "cost_usd": 0.1164,
                                             "ts": time.time(), "served_by": "claude-opus-5-5", "provider": "anthropic"})
        await bus.close()

    asyncio.run(kv())


def seed_journal(st: TableStore, today: date, days: list[date]) -> None:
    """A fortnight of round trips (engine and manual, wins and losses) through the real journal analytics."""
    from dhanbot.clock import epoch_at

    from tradingbots.bots.journal import analytics as JA

    fills, n = [], 0
    for i, d in enumerate(days[-9:] + [today]):
        for j, (sym, hh, move) in enumerate([("RELIANCE", "09:25", 6.0), ("INFY", "11:10", -4.0),
                                              ("HDFCBANK", "14:20", 3.0)]):
            if (i + j) % 4 == 3:
                continue
            n += 2
            src = "bot" if j != 1 else "manual"
            m = move * (1 if (i + j) % 3 else -1)
            t0 = epoch_at(d, hh)
            fills += [JA.Fill("real", "simulated", sym, "INTRADAY", f"T{n}", t0, 1, 10, 1500.0, 6.0, src, "NSE_EQ",
                              "entry" if src == "bot" else "", 1499.6 if src == "bot" else None),
                      JA.Fill("real", "simulated", sym, "INTRADAY", f"T{n + 1}", t0 + 1500 + 300 * j, -1, 10, 1500.0 + m,
                              6.5, src, "NSE_EQ", ("target" if m > 0 else "sl") if src == "bot" else "", None)]
    trips = JA.round_trips(fills)
    trips[-1]["tags"], trips[-1]["note"] = "breakout", "entered on the 15-minute high"
    st.replace("perf_trades", trips)
    st.replace("perf_summary", JA.summary_rows(trips, today))
    st.replace("perf_breakdown", JA.breakdown_rows(trips, today))


class FakeBots:
    """Answers command requests on the bus like the bots would, and records what was asked."""

    def __init__(self, base: Path, replies: dict[str, dict]) -> None:
        self.base, self.replies, self.seen = base, replies, []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)

    async def _run(self) -> None:
        bus = make_bus("sqlite", "fake-bots", data_dir=self.base / "data")
        await bus.connect()
        tl = list(self.replies)
        await bus.ensure_group("fake-bots", tl, start="latest")
        while not self._stop.is_set():
            for m in await bus.consume("fake-bots", tl, block_s=0.2):
                corr = m.data.pop("_corr", None)
                self.seen.append((m.topic, m.data))
                if corr:
                    await bus.send_reply(corr, self.replies[m.topic])
                await bus.ack("fake-bots", [m])
        await bus.close()

    def __enter__(self) -> "FakeBots":
        self.thread.start()
        time.sleep(0.3)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self.thread.join(5)


def replies(today: str) -> dict[str, dict]:
    report_md = "# Large caps steady; RELIANCE leads\n\n*midday report*\n\n| Stock | Trend |\n|---|---|\n| RELIANCE | up |"
    return {
        topics.cmd("money", "list_expenses"): {"ok": True, "expenses": [
            {"id": 1, "day": today, "category": "data plan", "platform": "dhan", "amount": 499.0, "note": "Dhan data"}]},
        topics.cmd("budget", "history"): {"ok": True, "days": [usage_sample(today)], "alerts": []},
        topics.cmd("ai_analyst", "status"): {"ok": True, "mode": "anthropic", "reason": "claude-opus-5-5, effort high",
                                             "model": "claude-opus-5-5", "effort": "high", "refusal_fallbacks": True,
                                             "budget_usd": 1.0, "spent_today_usd": 0.1164, "running": "", "last": {},
                                             "open_batches": 0, "schedule": ["08:45", "12:30", "15:45"],
                                             "calls_today": 1},
        topics.cmd("ai_analyst", "reports"): {"ok": True, "reports": [
            {"id": "r1", "ts": time.time(), "day": today, "kind": "midday", "status": "ok", "model": "claude-opus-5-5",
             "served_by": "claude-opus-5-5", "headline": "Large caps steady; RELIANCE leads", "usd": 0.1164,
             "error": "", "provider": "anthropic"}]},
        topics.cmd("ai_analyst", "report"): {"ok": True, "found": True, "report": {
            "id": "r1", "status": "ok", "markdown": report_md, "packet": {"kind": "midday", "stocks": []}}},
        topics.cmd("ai_analyst", "run"): {"ok": True, "started": True, "kind": "on_demand"},
        topics.cmd("config", "token_status"): {"ok": True, "platform": "simulated",
                                               "info": {"platform": "simulated", "version": 3, "updated": time.time()},
                                               "error": "", "seconds_left": None},
        topics.cmd("config", "history"): {"ok": True, "versions": [
            {"version": 3, "ts": time.time(), "author": "dashboard", "note": "budget",
             "changes": ["budget.daily_capital_limit_inr: 100000.0 -> 60000.0"]}]},
        topics.cmd("config", "update"): {"ok": True, "applied": True, "version": 4,
                                         "changes": ["budget.daily_capital_limit_inr: 60000.0 -> 75000.0"],
                                         "restart_required": False, "errors": []},
        topics.cmd("execution", "control"): {"ok": True, "done": True, "result": {"state": "PAUSED"}},
        topics.cmd("history", "refresh"): {"ok": True, "refreshed": 3},
        topics.cmd("journal", "annotate"): {"ok": True, "saved": True, "count": 1},
        topics.cmd("notifier", "status"): {"ok": True, "telegram": "on", "email": "off", "problems": [
            "email: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and EMAIL_TO in .env (see .env.example)"],
            "why": {"email": "email: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and EMAIL_TO in .env (see .env.example)"},
            "sent": {"telegram": 5, "email": 0, "suppressed": 2, "failed": 0}},
        topics.cmd("notifier", "test"): {"ok": True, "telegram": "sent",
                                         "email": "off - email: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and EMAIL_TO"},
        topics.cmd("notifier", "summary"): {"ok": True, "sent": True, "telegram": True, "email": False,
                                            "text": "📊 Daily summary Thu 05 Nov 2026\nAlerts: 1 warning, 0 errors"},
    }
