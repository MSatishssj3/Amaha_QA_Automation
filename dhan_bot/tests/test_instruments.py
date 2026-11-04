from __future__ import annotations

from datetime import date, timedelta

import pytest

from dhanbot.config import InstrumentCfg, load_config
from dhanbot.instruments import InstrumentError, InstrumentStore, _tick_paise
from dhanbot.models import Instrument
from dhanbot.sim.mock_dhan import MockDhan

TODAY = date(2026, 11, 4)  # a Wednesday


@pytest.fixture(scope="module")
def csv_text() -> str:
    m = MockDhan.__new__(MockDhan)
    from dhanbot.sim.mock_dhan import default_instruments

    m.instruments = default_instruments(TODAY)
    return MockDhan.instrument_csv(m)


@pytest.fixture(scope="module")
def store(csv_text) -> InstrumentStore:
    return InstrumentStore.from_csv_text(csv_text, freeze_qty={"NIFTY": 1800})


def test_header_validation():
    with pytest.raises(InstrumentError) as e:
        InstrumentStore.from_csv_text("A,B,C\n1,2,3\n")
    assert "missing columns" in str(e.value)


def test_tick_units():
    assert _tick_paise("5.0000", "auto") == 5 and _tick_paise("0.05", "auto") == 5
    assert _tick_paise("0.01", "auto") == 1 and _tick_paise("1.0", "paise") == 1
    assert _tick_paise("1.0", "rupees") == 100


def test_lookups(store):
    rel = store.by_trading_symbol("NSE_EQ", "reliance")
    assert rel and rel.security_id == 2885 and rel.tick_paise == 10 and rel.feed_code == 1
    assert store.get("NSE_EQ", 2885) is rel
    nifty = store.get("IDX_I", 13)
    assert nifty and not nifty.tradable
    assert [i.trading_symbol for i in store.search("HDFC")][:1] == ["HDFCBANK"]


def test_segment_filter(csv_text):
    eq_only = InstrumentStore.from_csv_text(csv_text, segments={"NSE_EQ"})
    assert {i.segment for i in eq_only.by_key.values()} == {"NSE_EQ"}


def test_options_resolution(store):
    exps = store.expiries("NIFTY")
    assert len(exps) == 2 and exps[0] == date(2026, 11, 10)  # next Tuesday
    atm = store.option("NIFTY", "CE", spot=24012.0, today=TODAY)
    assert atm.strike_paise == 2400000 and atm.option_type == "CE" and atm.lot_size == 75
    assert atm.freeze_qty == 1800 and atm.underlying == "NIFTY"
    assert store.option("NIFTY", "CE", spot=24030.0, today=TODAY).strike_paise == 2405000  # nearer to 24050
    up2 = store.option("NIFTY", "PE", spot=24012.0, strike="ATM+2", today=TODAY)
    assert up2.strike_paise == 2410000 and up2.option_type == "PE"
    dn1 = store.option("NIFTY", "CE", spot=24012.0, strike="ATM-1", expiry="next", today=TODAY)
    assert dn1.strike_paise == 2395000 and dn1.expiry == exps[1]
    assert store.option("NIFTY", "CE", spot=0, strike=23600, today=TODAY).strike_paise == 2360000
    with pytest.raises(InstrumentError):
        store.option("NIFTY", "CE", spot=24000, strike="ATM+500", today=TODAY)
    with pytest.raises(InstrumentError):
        store.option("NIFTY", "CE", spot=24000, today=TODAY + timedelta(days=60))
    fut = store.future("NIFTY", today=TODAY)
    assert fut.instrument_type == "FUTIDX"


def test_resolve_config_entries(store):
    assert store.resolve(InstrumentCfg(alias="R", segment="NSE_EQ", trading_symbol="RELIANCE")).security_id == 2885
    assert store.resolve(InstrumentCfg(alias="X", segment="IDX_I", security_id=99999)).segment == "IDX_I"
    with pytest.raises(InstrumentError) as e:
        store.resolve(InstrumentCfg(alias="R", segment="NSE_EQ", trading_symbol="RELIANC"))
    assert "RELIANCE" in str(e.value)  # suggestion offered


def test_round_to_tick():
    i = Instrument(1, "NSE_EQ", 1, "X", tick_paise=5)
    assert i.round_to_tick(253444.99) == 253445
    assert i.round_to_tick(253441, "up") == 253445 and i.round_to_tick(253449, "down") == 253445
    assert i.round_to_tick(253445, "up") == 253445


async def test_download_cache_and_fallback(tmp_path):
    mock = MockDhan(auto_ticks=False)
    ep = await mock.start()
    try:
        cfg = load_config(data={"paths": {"data_dir": str(tmp_path)}}, env={})
        s1 = await InstrumentStore.load(cfg, ep.instruments_csv_url, {"NSE_EQ", "IDX_I"}, TODAY)
        assert s1.by_trading_symbol("NSE_EQ", "INFY") and (tmp_path / "instruments" / "2026-11-04.csv").exists()
        s2 = await InstrumentStore.load(cfg, ep.instruments_csv_url, {"NSE_EQ", "IDX_I"}, TODAY)
        assert s2.source.endswith(".pkl") and len(s2) == len(s1)
        # next day, server unreachable -> fall back to yesterday's CSV
        s3 = await InstrumentStore.load(cfg, "http://127.0.0.1:1/x.csv", {"NSE_EQ"}, TODAY + timedelta(days=1))
        assert s3.source.endswith("2026-11-04.csv")
    finally:
        await mock.stop()
