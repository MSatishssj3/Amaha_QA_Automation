from __future__ import annotations

import pytest

from dhanbot.config import ChargesCfg
from dhanbot.models import Fill, Instrument, Product, Side
from dhanbot.positions import PositionBook, broker_pnl_inr
from dhanbot.ratelimit import AsyncLimiter, OrderRateLimiter, SlidingWindow


class FakeTime:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_sliding_window_is_strict():
    ft = FakeTime()
    w = SlidingWindow(10, 1.0, ft)
    assert all(w.try_take() for _ in range(10)) and not w.try_take()
    assert w.wait_time() == pytest.approx(1.0)
    ft.t += 0.5
    assert not w.try_take()  # a token bucket would already allow 5 more here
    ft.t += 0.51
    assert all(w.try_take() for _ in range(10)) and not w.try_take()


def test_order_limiter_per_second_minute_and_day():
    ft = FakeTime()
    lim = OrderRateLimiter(10, 250, 1000, ft)
    assert sum(lim.try_acquire() for _ in range(30)) == 10  # burst capped at 10/s
    times = [ft.t] * 10
    for _ in range(240):  # two minutes of heavy demand (0.5s steps)
        ft.t += 0.5
        for _ in range(30):
            if lim.try_acquire():
                times.append(ft.t)
    for i, t in enumerate(times):  # never more than 250 in any rolling minute, 10 in any second
        assert sum(1 for x in times[i:] if x < t + 60) <= 250
        assert sum(1 for x in times[i:] if x < t + 1) <= 10
    assert len(times) >= 490
    day = OrderRateLimiter(10, 250, 3, ft)
    ft.t += 100
    assert [day.try_acquire() for _ in range(5)] == [True, True, True, False, False]
    assert day.wait_time() == float("inf")


def test_order_limiter_cooldown():
    ft = FakeTime()
    lim = OrderRateLimiter(10, 250, 1000, ft)
    lim.cooldown(1.0)
    assert not lim.try_acquire() and lim.wait_time() == pytest.approx(1.0)
    ft.t += 1.01
    assert lim.sec.available() == 10
    assert lim.try_acquire()


async def test_async_limiter():
    lim = AsyncLimiter(1000)
    for _ in range(5):
        await lim.acquire()


INST = Instrument(2885, "NSE_EQ", 1, "RELIANCE", tick_paise=10)
OPT = Instrument(40001, "NSE_FNO", 2, "NIFTY-CE", lot_size=75, tick_paise=5, option_type="CE")


def fill(side: Side, qty: int, px_rupees: float, inst: Instrument = INST) -> Fill:
    return Fill(1, inst, Product.INTRADAY, side, qty, px_rupees * 100, "MAIN", 0.0)


def test_position_long_round_trip_with_charges():
    book = PositionBook(ChargesCfg(brokerage_per_order_inr=20, statutory_pct=0.0))
    assert book.apply_fill(fill(Side.BUY, 10, 100.0), True) is None
    assert book.apply_fill(fill(Side.BUY, 10, 110.0), True) is None
    pos = book.get(INST, Product.INTRADAY)
    assert pos.qty == 20 and pos.avg_paise == pytest.approx(10500)
    assert book.mark(INST.key, 11000) is True
    assert pos.unrealised_paise == pytest.approx(20 * 500)
    trade = book.apply_fill(fill(Side.SELL, 20, 120.0), True)
    assert trade is not None and trade.qty == 20
    assert trade.pnl_paise == pytest.approx(20 * 1500 - 3 * 2000)  # 300 profit - 3 orders x 20
    assert pos.qty == 0 and pos.unrealised_paise == 0
    assert book.net_paise() == pytest.approx(30000 - 6000)


def test_position_short_partial_and_flip():
    book = PositionBook(ChargesCfg(brokerage_per_order_inr=0, statutory_pct=0.0))
    book.apply_fill(fill(Side.SELL, 75, 50.0, OPT), True)
    pos = book.get(OPT, Product.INTRADAY)
    assert pos.qty == -75
    assert book.apply_fill(fill(Side.BUY, 25, 40.0, OPT), False) is None
    assert pos.realised_paise == pytest.approx(25 * 1000)
    trade = book.apply_fill(fill(Side.BUY, 100, 45.0, OPT), False)  # closes 50, flips +50
    assert trade.pnl_paise == pytest.approx(25 * 1000 + 50 * 500)
    assert pos.qty == 50 and pos.avg_paise == pytest.approx(4500)
    assert book.open_positions() == [pos]
    assert book.gross_exposure_paise() == pytest.approx(50 * 4500)


def test_statutory_charges_estimate():
    book = PositionBook(ChargesCfg(brokerage_per_order_inr=20, statutory_pct=0.03))
    c = book.estimate_charges(10, 100000, INST, first_fill_of_order=True)
    assert c == pytest.approx(10 * 100000 * 0.0003 + 2000)


def test_reconcile_drift_and_adopt():
    book = PositionBook(ChargesCfg())
    book.apply_fill(fill(Side.BUY, 5, 100.0), True)
    rows = [{"exchangeSegment": "NSE_EQ", "securityId": "2885", "productType": "INTRADAY", "netQty": 7,
             "costPrice": 101.0, "realizedProfit": 10.0, "unrealizedProfit": -2.5}]
    drift = book.reconcile(rows, lambda seg, sid: INST if sid == 2885 else None)
    assert drift and book.qty(INST) == 7 and book.get(INST, Product.INTRADAY).avg_paise == 10100
    assert book.reconcile(rows, lambda seg, sid: INST) == []
    assert book.reconcile([], lambda seg, sid: INST) and book.qty(INST) == 0
    assert broker_pnl_inr(rows) == pytest.approx(7.5)
