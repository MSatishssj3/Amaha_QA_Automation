from __future__ import annotations

from datetime import date

import pytest

from dhanbot.bars import BarAggregator, parse_timeframe
from dhanbot.clock import epoch_at
from dhanbot.indicators import ATR, EMA, RSI, SMA, VWAP, RollingMax, RollingMin

DAY = date(2026, 11, 4)
OPEN = epoch_at(DAY, "09:15")
KEY = (1, 2885)


def agg() -> BarAggregator:
    return BarAggregator(lambda t: OPEN, grace_s=1.0)


def test_parse_timeframe():
    assert parse_timeframe("5s") == 5 and parse_timeframe("1m") == 60 and parse_timeframe("15m") == 900
    assert parse_timeframe("1h") == 3600 and parse_timeframe("1d") == 86400
    with pytest.raises(ValueError):
        parse_timeframe("5x")


def test_bars_align_to_session_open_and_track_volume():
    a = agg()
    s5 = a.add(KEY, "R", "5m")
    s1 = a.add(KEY, "R", "1m")
    assert a.add(KEY, "R", "1m") is s1
    assert a.on_tick(KEY, OPEN + 1, 100.0, 1000) == []
    a.on_tick(KEY, OPEN + 30, 102.0, 1500)
    a.on_tick(KEY, OPEN + 59, 99.0, 1600)
    closed = a.on_tick(KEY, OPEN + 61, 101.0, 1700)
    assert len(closed) == 1 and closed[0].tf == 60
    b = closed[0]
    assert (b.start, b.open, b.high, b.low, b.close, b.volume) == (OPEN, 100.0, 102.0, 99.0, 99.0, 600)
    assert s1.close[-1] == 99.0 and len(s5) == 0
    closed = a.on_tick(KEY, OPEN + 300, 105.0, 2000)
    assert {x.tf for x in closed} == {60, 300}
    five = [x for x in closed if x.tf == 300][0]
    assert five.start == OPEN and five.high == 102.0 and five.close == 101.0 and five.volume == 700


def test_timer_flush_closes_bars_without_ticks_and_late_ticks_ignored():
    a = agg()
    s = a.add(KEY, "R", "1m")
    a.on_tick(KEY, OPEN + 10, 100.0, None)
    assert a.flush(OPEN + 60.5) == []
    closed = a.flush(OPEN + 61.1)
    assert len(closed) == 1 and len(s) == 1
    assert a.on_tick(KEY, OPEN + 59, 50.0, None) == [] and s.cur is None  # late tick for a closed bar


def test_attach_indicator_with_history():
    a = agg()
    s = a.add(KEY, "R", "1m")
    from dhanbot.models import Bar

    s.load_history([Bar(KEY, "R", 60, OPEN - 600 + i * 60, 1, 2, 0.5, float(i), 10) for i in range(10)])
    ema = s.attach(EMA(3))
    atr = s.attach(ATR(3), "hlc")
    assert ema.ready and ema.value == pytest.approx(8.0, abs=0.2)
    assert atr.ready
    assert s.last().close == 9.0


def test_ema_sma_rsi_reference_values():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
              46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    rsi = RSI(14)
    for c in closes[:15]:
        rsi.update(c)
    assert rsi.value == pytest.approx(70.46, abs=0.1)  # classic Wilder example
    sma = SMA(3)
    assert [sma.update(x) for x in (1, 2, 3, 4)] == [None, None, 2.0, 3.0]
    ema = EMA(3)
    vals = [ema.update(x) for x in (1, 2, 3, 4)]
    assert vals[:2] == [None, None] and vals[2] == 2.0 and vals[3] == pytest.approx(3.0)


def test_vwap_and_rolling():
    v = VWAP()
    v.update(100, 10)
    v.update(110, 30)
    assert v.value == pytest.approx(107.5)
    v.reset()
    assert v.value is None
    mx, mn = RollingMax(3), RollingMin(3)
    out = [(mx.update(x), mn.update(x)) for x in (5, 1, 4, 2, 3, 9)]
    assert out[-1] == (9, 2) and out[2] == (5, 1) and out[3] == (4, 1)
