"""Daily history / trade history / ledger endpoints, the risk plug-in hook and the single-instance lock."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dhanbot.clock import ist_datetime
from dhanbot.config import load_config
from dhanbot.dhan.rest import DhanRest
from dhanbot.lock import InstanceLock
from dhanbot.models import Order, Side
from dhanbot.risk import Decision
from dhanbot.sim.mock_dhan import MockDhan

from .helpers import wait_for
from .test_risk import order as risk_order
from .test_risk import setup as risk_setup

CID = "1000000001"


@pytest.fixture
async def mock_rest():
    mock = MockDhan(client_id=CID, auto_ticks=False)
    ep = await mock.start()
    rest = DhanRest(ep, CID, mock.token)
    await rest.start()
    yield mock, rest
    await rest.close()
    await mock.stop()


async def test_historical_daily_candles(mock_rest):
    mock, rest = mock_rest
    today = ist_datetime(mock.now()).date()
    res = await rest.historical_daily(2885, "NSE_EQ", "EQUITY", (today - timedelta(days=60)).isoformat(),
                                      today.isoformat())
    assert res.ok
    d = res.data
    n = len(d["close"])
    assert 35 <= n <= 45  # ~42 weekdays in 60 days, today excluded
    assert all(len(d[k]) == n for k in ("open", "high", "low", "volume", "timestamp"))
    assert all(lo <= min(o, c) and hi >= max(o, c) for o, hi, lo, c in zip(d["open"], d["high"], d["low"], d["close"],
                                                                             strict=True))
    days = [ist_datetime(t).date() for t in d["timestamp"]]
    assert days == sorted(days) and all(x.weekday() < 5 for x in days) and days[-1] < today
    assert abs(d["close"][-1] - mock.instrument("RELIANCE").prev_close) < 0.01  # continuous with "yesterday"
    again = await rest.historical_daily(2885, "NSE_EQ", "EQUITY", (today - timedelta(days=60)).isoformat(),
                                        today.isoformat())
    assert again.data == d  # deterministic


async def test_trade_history_with_charges_and_ledger(mock_rest):
    mock, rest = mock_rest
    inst = mock.instrument("RELIANCE")
    body = rest.dumps({"transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY",
                       "orderType": "LIMIT", "validity": "DAY", "securityId": "2885", "quantity": 10,
                       "price": round(inst.ask + 1, 1), "correlationId": "X1"})
    assert (await rest.place_order_raw(body)).ok
    await wait_for(lambda: mock.trades)
    mock.set_price("RELIANCE", inst.price + 5)
    sell = rest.dumps({"transactionType": "SELL", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY",
                       "orderType": "MARKET", "validity": "DAY", "securityId": "2885", "quantity": 10, "price": 0})
    assert (await rest.place_order_raw(sell)).ok
    await wait_for(lambda: len(mock.trades) == 2)
    today = ist_datetime(mock.now()).date().isoformat()
    hist = await rest.get_trade_history(today, today, 0)
    assert hist.ok and len(hist.data) == 2
    t = hist.data[0]
    assert t["transactionType"] == "BUY" and t["tradedQuantity"] == 10 and t["exchangeSegment"] == "NSE_EQ"
    assert t["brokerageCharges"] > 0 and t["stampDuty"] > 0 and t["stt"] == 0  # intraday STT is on the sell side
    assert hist.data[1]["stt"] > 0
    assert (await rest.get_trade_history(today, today, 1)).data == []
    book = await rest.get_trades()
    assert len(book.data) == 2
    led = await rest.get_ledger(today, today)
    narr = [e["narration"] for e in led.data]
    assert narr[0].startswith("FUNDS ADDED") and any(n.startswith("Charges") for n in narr)
    assert any(n.startswith("Intraday settlement") for n in narr)
    assert float(led.data[-1]["runbal"]) == pytest.approx(
        sum(float(e["credit"]) - float(e["debit"]) for e in led.data), abs=0.02)
    mock.withdraw(1000)
    mock.add_holding("INFY", 5, 1500.0)
    assert (await rest.get_holdings()).data[0]["totalQty"] == 5
    assert (await rest.get_ledger(today, today)).data[-1]["narration"] == "FUNDS WITHDRAWN"
    old = (date.fromisoformat(today) - timedelta(days=10)).isoformat()
    assert (await rest.get_ledger(old, old)).data == []


def test_extra_risk_checks_hook():
    cfg, clock, market, book, ks, risk, working = risk_setup()
    seen = []

    def budget(order: Order, record: bool):
        seen.append(record)
        return Decision(False, "budget_daily_capital: limit reached") if order.qty > 5 else None

    risk.extra_checks.append(budget)
    assert risk.check(risk_order(qty=3)).ok
    d = risk.check(risk_order(qty=6, price_paise=145100))
    assert not d.ok and d.reason.startswith("budget_daily_capital")
    assert risk.rejections["budget_daily_capital"] == 1
    risk.check(risk_order(qty=2, price_paise=145200), record=False)
    assert seen == [True, True, False]
    exit_order = risk_order(side=Side.SELL, qty=1)
    from dhanbot.models import Fill, Intent, Product

    book.apply_fill(Fill(1, exit_order.instrument, Product.INTRADAY, Side.BUY, 10, 145000, "MAIN", 0), True)
    exit_order.intent = Intent.EXIT
    n = len(seen)
    assert risk.check(exit_order).ok and len(seen) == n  # exits never consult plug-ins

    def broken(order, record):
        raise RuntimeError("boom")

    risk.extra_checks[:] = [broken]
    d = risk.check(risk_order(qty=1, price_paise=145300))
    assert not d.ok and d.reason.startswith("extra_check")  # fails closed


def test_instance_lock(tmp_path):
    a, b = InstanceLock(tmp_path / "x.lock"), InstanceLock(tmp_path / "x.lock")
    assert a.acquire() and a.held
    assert not b.acquire()
    import os

    assert b.holder_pid() == str(os.getpid())
    a.release()
    assert b.acquire()
    b.release()


async def test_engine_refuses_second_instance(tmp_path):
    from dhanbot.clock import SimClock
    from dhanbot.engine import Engine
    from dhanbot.errors import PreflightError

    from .test_engine_live import sim_start

    clock = SimClock(sim_start(), speed=20)
    mock = MockDhan(clock=clock, client_id=CID)
    mock.ips["primaryIP"] = "203.0.113.10"
    ep = await mock.start()
    data = {"paths": {"data_dir": str(tmp_path)}, "perf": {"gc_freeze": False},
            "instruments": [{"alias": "RELIANCE", "segment": "NSE_EQ", "trading_symbol": "RELIANCE"}]}
    env = {"DHAN_CLIENT_ID": CID, "DHAN_ACCESS_TOKEN": mock.token}
    e1 = Engine(load_config(data=data, env=env), clock=clock, endpoints=ep, use_control=False, use_telegram=False)
    e2 = Engine(load_config(data=data, env=env), clock=clock, endpoints=ep, use_control=False, use_telegram=False)
    try:
        await e1.start()
        with pytest.raises(PreflightError) as ei:
            await e2.start()
        assert "already running" in str(ei.value)
        await e2.shutdown()
        await e1.shutdown()
        e3 = Engine(load_config(data=data, env=env), clock=clock, endpoints=ep, use_control=False, use_telegram=False)
        await e3.start()  # lock released by e1's shutdown
        await e3.shutdown()
    finally:
        for e in (e1, e2):
            if not e._shutting_down:
                await e.shutdown()
        await mock.stop()
