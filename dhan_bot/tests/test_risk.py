"""One test per risk rule + kill-switch transitions + control channel."""

from __future__ import annotations

import pytest

from dhanbot.clock import VirtualClock
from dhanbot.config import InstrumentCfg, load_config
from dhanbot.control import ControlServer, send_command
from dhanbot.dhan.packets import Tick
from dhanbot.market_state import MarketState
from dhanbot.models import Fill, Instrument, Intent, Order, OrderKind, OrderType, Product, Side
from dhanbot.positions import ClosedTrade, PositionBook
from dhanbot.risk import KillSwitch, RiskEngine, TradingState

EQ = Instrument(2885, "NSE_EQ", 1, "RELIANCE", tick_paise=10)
OPT = Instrument(40001, "NSE_FNO", 2, "NIFTY-CE", lot_size=75, tick_paise=5, option_type="CE", freeze_qty=1800)
IDX = Instrument(13, "IDX_I", 0, "NIFTY")


def setup(risk=None, window=True, token_left=1e9, **cfgkw):
    data = {"risk": {"max_daily_loss_inr": 1000, **(risk or {})}, **cfgkw}
    cfg = load_config(data=data, env={})
    clock = VirtualClock(1_780_000_000.0)
    market = MarketState()
    for i in (EQ, OPT, IDX):
        market.register(i)
    book = PositionBook(cfg.charges)
    ks = KillSwitch(clock)
    limits = {EQ.key: InstrumentCfg("RELIANCE", "NSE_EQ", "RELIANCE", max_qty=100),
              OPT.key: InstrumentCfg("CE", "NSE_FNO", "NIFTY-CE", max_qty=3000),
              IDX.key: InstrumentCfg("NIFTY", "IDX_I", security_id=13, tradable=False)}
    working = {"qty": 0, "entries": 0}
    risk = RiskEngine(cfg, book, market, clock, ks, limits=limits, in_entry_window=lambda now: window,
                      token_seconds_left=lambda: token_left, working_exit_qty=lambda inst: working["qty"],
                      working_entries=lambda inst: working["entries"])
    price(market, clock, EQ, 1450.0)
    price(market, clock, OPT, 100.0)
    price(market, clock, IDX, 24000.0)
    return cfg, clock, market, book, ks, risk, working


def price(market, clock, inst, ltp):
    market.on_tick(Tick(2, inst.feed_code, inst.security_id, ltp, 0), clock.time(), clock.time())


def order(inst=EQ, side=Side.BUY, qty=10, price_paise=145000, **kw):
    return Order(1, inst, side, qty, price_paise=price_paise, **kw)


def reason(d):
    assert not d.ok, "expected rejection"
    return d.reason.split(":", 1)[0]


def test_accepts_a_normal_entry():
    *_, risk, _ = setup()
    assert risk.check(order()).ok and risk.orders_today == 1


@pytest.mark.parametrize("o,expected", [
    (order(qty=0), "qty"),
    (order(OPT, qty=50, price_paise=10000), "lot"),
    (order(IDX, qty=1, price_paise=2400000), "whitelist"),
    (order(qty=101), "max_qty"),
    (order(OPT, qty=1875, price_paise=10000), "freeze_qty"),
    (order(price_paise=150000), "price_band"),  # 3.4% away
    (order(OPT, qty=75, price_paise=12000), "price_band"),  # 20% away (options band 10%)
    (order(qty=80, price_paise=145000), "order_value"),  # 1.16 lakh > 1 lakh
    (order(kind=OrderKind.SUPER, target_paise=144000, sl_paise=143000), "bracket"),
    (order(kind=OrderKind.SUPER, target_paise=146000, sl_paise=146500), "bracket"),
])
def test_entry_rules(o, expected):
    *_, risk, _ = setup()
    assert reason(risk.check(o)) == expected


def test_time_window_token_and_stale_feed():
    *_, risk, _ = setup(window=False)
    assert reason(risk.check(order())) == "time_window"
    *_, risk, _ = setup(token_left=60)
    assert reason(risk.check(order())) == "token"
    cfg, clock, market, book, ks, risk, _ = setup()
    clock.advance(cfg.feed.stale_after_s + 1)
    assert reason(risk.check(order())) == "stale_feed"


def test_open_positions_exposure_and_margin():
    cfg, clock, market, book, ks, risk, _ = setup({"max_open_positions": 1, "max_gross_exposure_inr": 20000})
    book.apply_fill(Fill(1, OPT, Product.INTRADAY, Side.BUY, 75, 10000, "MAIN", 0), True)
    assert reason(risk.check(order(qty=5))) == "max_open_positions"
    cfg, clock, market, book, ks, risk, _ = setup({"max_gross_exposure_inr": 20000})
    book.apply_fill(Fill(1, OPT, Product.INTRADAY, Side.BUY, 75, 10000, "MAIN", 0), True)
    assert reason(risk.check(order(qty=10))) == "exposure"  # 7.5k + 14.5k
    cfg, clock, market, book, ks, risk, _ = setup()
    risk.funds_available_inr = 2000.0
    assert reason(risk.check(order(qty=10))) == "margin"  # 14.5k * 0.2 * 1.1 = 3.19k
    risk.funds_available_inr = 5000.0
    assert risk.check(order(qty=10)).ok


def test_duplicate_and_daily_counts():
    cfg, clock, market, book, ks, risk, _ = setup({"max_orders_per_day": 3, "max_trades_per_instrument": 2})
    assert risk.check(order()).ok
    assert reason(risk.check(order())) == "duplicate"
    clock.advance(1)
    price(market, clock, EQ, 1450.0)
    assert risk.check(order()).ok
    clock.advance(1)
    price(market, clock, EQ, 1450.0)
    assert reason(risk.check(order(qty=5))) == "max_trades_per_instrument"
    assert risk.check(order(OPT, qty=75, price_paise=10000)).ok
    assert reason(risk.check(order(OPT, qty=150, price_paise=10000))) == "max_orders_per_day"


def test_exits_are_reduce_only_and_bypass_limits():
    cfg, clock, market, book, ks, risk, working = setup({"max_orders_per_day": 1})
    exit_long = order(side=Side.SELL, qty=5, intent=Intent.EXIT)
    assert reason(risk.check(exit_long)) == "reduce_only"  # nothing to exit
    book.apply_fill(Fill(1, EQ, Product.INTRADAY, Side.BUY, 10, 145000, "MAIN", 0), True)
    ks.halt("test")
    risk.orders_today = 99
    assert risk.check(exit_long).ok  # halted + over the daily cap, exit still allowed
    assert reason(risk.check(order(side=Side.SELL, qty=11, intent=Intent.EXIT))) == "reduce_only"
    working["qty"] = 8
    assert reason(risk.check(exit_long)) == "reduce_only"  # 5 + 8 working > 10
    assert reason(risk.check(order(side=Side.BUY, qty=1, intent=Intent.EXIT))) == "reduce_only"
    far = order(side=Side.SELL, qty=1, price_paise=130000, intent=Intent.EXIT)
    working["qty"] = 0
    assert reason(risk.check(far)) == "price_band"  # 10% below even with the doubled exit band
    assert risk.check(order(side=Side.SELL, qty=1, order_type=OrderType.MARKET, price_paise=0,
                            intent=Intent.EXIT)).ok


def test_daily_loss_halts_and_profit_lock_pauses():
    cfg, clock, market, book, ks, risk, _ = setup({"daily_profit_lock_inr": 500})
    states = []
    ks.listeners.append(lambda s, r: states.append(s))
    book.apply_fill(Fill(1, EQ, Product.INTRADAY, Side.BUY, 10, 145000, "MAIN", 0), True)
    book.mark(EQ.key, 145000 + 6000)  # +600 unrealised, -20 brokerage
    risk.check_pnl()
    assert ks.state is TradingState.PAUSED
    assert reason(risk.check(order(OPT, qty=75, price_paise=10000))) == "killswitch"
    assert ks.resume() and ks.state is TradingState.RUNNING
    book.mark(EQ.key, 145000 - 10000)  # -1000 - charges
    risk.check_pnl()
    assert ks.state is TradingState.HALTED and "daily loss" in ks.reason
    assert not ks.resume() and ks.state is TradingState.HALTED  # latched
    assert states == [TradingState.PAUSED, TradingState.RUNNING, TradingState.HALTED]


def test_consecutive_losses_pause_and_reject_storm_halts():
    cfg, clock, market, book, ks, risk, _ = setup({"max_consecutive_losses": 2, "reject_storm_count": 3})
    t = ClosedTrade(EQ, Product.INTRADAY, -100, 1)
    risk.on_trade_closed(t)
    risk.on_trade_closed(ClosedTrade(EQ, Product.INTRADAY, 50, 1))
    risk.on_trade_closed(t)
    assert ks.state is TradingState.RUNNING
    risk.on_trade_closed(t)
    assert ks.state is TradingState.PAUSED
    o = order()
    o.reason = "RMS reject"
    for _ in range(3):
        risk.on_broker_reject(o)
    assert ks.state is TradingState.HALTED and "reject storm" in ks.reason


def test_manual_trades_pnl_source():
    cfg, clock, market, book, ks, risk, _ = setup({"include_manual_trades": True})
    risk.broker_pnl_inr = -1500.0
    risk.check_pnl()
    assert ks.state is TradingState.HALTED


async def test_control_channel(tmp_path):
    calls = []
    srv = ControlServer(tmp_path / "control.json", {"status": lambda req: {"state": "RUNNING"},
                                                    "pause": lambda req: calls.append(req) or {"paused": True}})
    await srv.start()
    try:
        assert (await send_command(tmp_path / "control.json", "status")) == {"ok": True, "state": "RUNNING"}
        assert (await send_command(tmp_path / "control.json", "pause", why="x"))["paused"]
        assert calls[0]["why"] == "x"
        assert not (await send_command(tmp_path / "control.json", "nope"))["ok"]
        import orjson

        info = orjson.loads((tmp_path / "control.json").read_bytes())
        (tmp_path / "bad.json").write_bytes(orjson.dumps({"port": info["port"], "token": "wrong"}))
        assert (await send_command(tmp_path / "bad.json", "status"))["error"] == "bad token"
    finally:
        await srv.stop()
    assert not (await send_command(tmp_path / "control.json", "status"))["ok"]


def test_one_working_entry_per_instrument():
    cfg, clock, market, book, ks, risk, working = setup()
    working["entries"] = 1
    assert reason(risk.check(order())) == "working_entry"
    working["entries"] = 0
    assert risk.check(order()).ok
