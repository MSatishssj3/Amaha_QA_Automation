"""History metrics + money analytics (reference values) and the tables -> history -> money flow end to end."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from tradingbots.bots.history import metrics as M
from tradingbots.bots.money import analytics as A
from tradingbots.core import topics
from tradingbots.core.tables_io import TableReader

from .helpers import wait_for
from .team import Team


def days(n: int, start: date = date(2025, 1, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_metrics_on_a_steady_uptrend():
    n = 300
    ds = days(n)
    c = [100.0 * (1.001 ** i) for i in range(n)]  # +0.1%/day, no down days
    h = [x * 1.005 for x in c]
    lo = [x * 0.995 for x in c]
    v = [1000] * (n - 1) + [3000]
    m = M.stock_metrics("UP", ds, c, h, lo, c, v)
    assert m["ret_1d"] == pytest.approx(0.1, abs=1e-6)
    assert m["ret_1m"] == pytest.approx((1.001 ** 21 - 1) * 100, rel=1e-9)
    assert m["rsi14"] == 100.0 and m["max_dd_1y"] == 0.0
    assert m["sma20"] == pytest.approx(np.mean(c[-20:]))
    assert m["dist_sma50"] > 0 and m["dist_sma200"] > 0 and m["macd"] > 0
    assert m["from_high_52w"] == pytest.approx((c[-1] / h[-1] - 1) * 100)
    assert m["vol_ratio20"] == pytest.approx(3.0)
    assert m["adx14"] > 25 and m["trend"] == "strong_up"
    assert m["trend_score"] == pytest.approx(100 * (3 + (1.001 ** 63 - 1) * 10) / 4, abs=0.1)  # momentum term 0.65
    assert m["cagr"] == pytest.approx((1.001 ** 252 - 1) * 100, rel=1e-6)
    assert m["vol_ann"] == pytest.approx(0.0, abs=1e-6)


def test_metrics_downtrend_drawdown_crosses_and_beta():
    n = 320
    ds = days(n)
    up = [100 + i * 0.5 for i in range(160)]
    down = [up[-1] - (i + 1) * 0.6 for i in range(160)]
    c = up + down
    m = M.stock_metrics("DN", ds, c, [x + 1 for x in c], [x - 1 for x in c], c, [1000] * n)
    assert m["trend"] in ("down", "strong_down") and m["trend_score"] < 0
    assert m["max_dd_1y"] == pytest.approx((c[-1] / max(c[-252:]) - 1) * 100)
    assert m["death_cross_on"] is not None and m["death_cross_on"] > ds[160]
    bench = dict(zip(ds, c, strict=True))
    m2 = M.stock_metrics("DN", ds, c, [x + 1 for x in c], [x - 1 for x in c], c, [1000] * n, bench)
    assert m2["beta_1y"] == pytest.approx(1.0, abs=1e-9) and m2["rs_3m"] == pytest.approx(0.0, abs=1e-9)


def test_rsi_wilder_reference():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert M.rsi(np.array(closes), 14) == pytest.approx(70.46, abs=0.1)


def test_monthly_returns_and_correlations():
    ds = [date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 31)]
    mr = M.monthly_returns("X", ds, [100.0, 110.0, 99.0])
    assert [round(r["ret"], 6) for r in mr] == [10.0, -10.0] and mr[0]["month"] == 2
    d = days(60)
    a = {x: 100 + i + (i % 3) for i, x in enumerate(d)}
    b = {x: 2 * v for x, v in a.items()}
    corr = {(r["symbol_a"], r["symbol_b"]): r["corr"] for r in M.correlations({"A": a, "B": b})}
    assert corr[("A", "B")] == pytest.approx(1.0) and corr[("A", "A")] == 1.0


def t(side, qty, price, ts, sym="RELIANCE", charges=0.0, platform="dhan"):
    return {"platform": platform, "symbol": sym, "side": side, "qty": qty, "price": price, "trade_ts": ts,
            "day": date(2026, 11, 4), "value": qty * price, "total_charges": charges, "brokerage": charges}


def test_fifo_realized_intraday_partial_short_and_holding_cost():
    trades = [t("BUY", 10, 100, 1), t("BUY", 10, 110, 2), t("SELL", 15, 120, 3),  # 10@100 + 5@110 closed
              t("SELL", 5, 90, 1, sym="INFY"),  # sold from an old holding at avg cost 80
              t("SELL", 3, 50, 1, sym="SHORTY"), t("BUY", 3, 45, 2, sym="SHORTY")]  # short covered +15
    rows, open_lots, notes = A.fifo_realized(trades, {("dhan", "INFY"): 80.0})
    r = {x["symbol"]: x["realized"] for x in rows}
    assert r["RELIANCE"] == pytest.approx(10 * 20 + 5 * 10) and r["INFY"] == pytest.approx(50.0)
    assert r["SHORTY"] == pytest.approx(15.0)
    assert open_lots[("dhan", "RELIANCE")] == (5, 110.0)
    assert any("SHORTY" in n for n in notes)


def test_allocation_charges_cashflows_xirr():
    alloc = A.allocation([{"platform": "dhan", "symbol": "INFY", "qty": 10, "avg_cost": 100.0}],
                         [{"platform": "dhan", "symbol": "RELIANCE", "net_qty": -5, "cost_price": 200.0,
                           "product": "INTRADAY", "unrealized": 25.0}],
                         {"INFY": 120.0, "RELIANCE": 195.0}, {"INFY": "IT"})
    infy = [a for a in alloc if a["symbol"] == "INFY"][0]
    assert infy["current_value"] == 1200.0 and infy["pnl"] == 200.0 and infy["pnl_pct"] == 20.0 and infy["sector"] == "IT"
    assert round(sum(a["weight_pct"] for a in alloc)) == 100
    ch = A.charges_by_month([t("BUY", 10, 100, 1, charges=5.0), t("SELL", 10, 110, 2, charges=6.0)])
    assert ch[0]["total"] == 11.0 and ch[0]["turnover"] == 2100.0 and ch[0]["month"] == "2026-11"
    cf = A.cashflows_by_month([{"platform": "dhan", "day": date(2026, 1, 5), "category": "deposit", "credit": 1000},
                               {"platform": "dhan", "day": date(2026, 1, 20), "category": "withdrawal", "debit": 300}])
    assert cf == [{"platform": "dhan", "month": "2026-01", "deposits": 1000.0, "withdrawals": 300.0, "net": 700.0}]
    assert A.xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)]) == pytest.approx(10.0, abs=0.05)
    assert A.xirr([(date(2025, 1, 1), -100.0)]) is None


async def test_tables_history_money_flow(tmp_path):
    team = Team(tmp_path, ["sim_market", "config", "market_data", "tables", "history", "money"], speed=30)
    async with team:
        c = team.client
        reader = TableReader(team.paths.tables)
        mock = team.bots["sim_market"].mock
        # an intraday round trip at the simulated broker -> trades with charges -> realised P&L
        rel = mock.instrument("RELIANCE")
        from dhanbot.dhan.rest import DhanRest

        rest = DhanRest(mock.endpoints(), mock.client_id, mock.token)
        await rest.start()
        for side, otype, price in (("BUY", "LIMIT", round(rel.ask + 2, 1)), ("SELL", "MARKET", 0)):
            r = await rest.place_order_raw(rest.dumps({"transactionType": side, "exchangeSegment": "NSE_EQ",
                                                       "productType": "INTRADAY", "orderType": otype, "validity": "DAY",
                                                       "securityId": "2885", "quantity": 4, "price": price}))
            assert r.ok
            await wait_for(lambda: len(mock.trades) >= (1 if side == "BUY" else 2), 5)
        await rest.close()
        await c.request(topics.cmd("market_data", "snapshot"), {"full": True}, timeout=20)

        def metrics_ready():
            return reader.count("history_metrics") >= 4  # NIFTY + 3 stocks (+ holdings)

        assert await wait_for(metrics_ready, 40), team.bots["history"].last_run
        hm = {r["symbol"]: r for r in reader.table("history_metrics").to_dicts()}
        assert {"RELIANCE", "HDFCBANK", "INFY", "NIFTY"} <= set(hm)
        assert hm["RELIANCE"]["days"] > 400 and hm["RELIANCE"]["trend"] in (
            "strong_up", "up", "sideways", "down", "strong_down")
        assert hm["RELIANCE"]["beta_1y"] is not None and hm["NIFTY"]["beta_1y"] is None
        assert reader.count("monthly_returns") > 20 and reader.count("correlations") == 9

        def money_ready():
            return reader.count("money_pnl") > 0 and reader.count("money_charges") > 0

        assert await wait_for(money_ready, 30)
        assert reader.query("SELECT SUM(total) AS t FROM money_charges")["t"][0] > 0
        trades = reader.table("trades")
        assert trades.height == 2 and set(trades["source"].to_list()) == {"manual"}
        alloc = {r["symbol"]: r for r in reader.table("money_allocation").to_dicts()}
        assert {"INFY", "HDFCBANK"} <= set(alloc) and alloc["INFY"]["kind"] == "holding"
        add = await c.request(topics.cmd("money", "add_expense"), {"amount": 499, "category": "data subscription",
                                                                   "platform": "dhan", "note": "Data API plan"})
        assert add["added"]
        exp = reader.table("money_expenses")
        assert 499.0 in exp["amount"].to_list()
        summ = {r["metric"]: r["value"] for r in reader.table("money_summary").to_dicts()}
        assert summ["expenses_mtd"] >= 499 and summ["net_deposits"] > 0 and summ["charges_mtd"] > 0
        lst = await c.request(topics.cmd("tables", "list"), {})
        assert lst["tables"]["candles_daily"]["partitions"] >= 4
