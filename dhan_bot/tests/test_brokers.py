"""Paper broker fill model and the live broker + OMS against the mock Dhan (incl. fault recovery)."""

from __future__ import annotations

import asyncio

import orjson
import pytest

from dhanbot.broker.live import LiveBroker
from dhanbot.broker.paper import PaperBroker
from dhanbot.clock import VirtualClock
from dhanbot.config import load_config
from dhanbot.dhan.order_ws import OrderUpdateConnection
from dhanbot.dhan.rest import DhanRest
from dhanbot.dhan.packets import Tick
from dhanbot.market_state import MarketState
from dhanbot.models import Instrument, Intent, OrderKind, OrderStatus, OrderType, Side
from dhanbot.oms import OrderManager
from dhanbot.positions import PositionBook
from dhanbot.ratelimit import OrderRateLimiter
from dhanbot.sim.mock_dhan import MockDhan

from .helpers import wait_for

INST = Instrument(2885, "NSE_EQ", 1, "RELIANCE", tick_paise=10)


# ---------------------------------------------------------------------------------------------
# paper broker (deterministic virtual clock)
# ---------------------------------------------------------------------------------------------


def paper_setup(slippage=0, ack_ms=30):
    cfg = load_config(data={"paper": {"slippage_ticks": slippage, "ack_ms": ack_ms},
                            "execution": {"entry_ttl_ms": 0}}, env={})
    clock = VirtualClock(1_780_000_000.0)
    market = MarketState()
    st = market.register(INST)
    broker = PaperBroker(market, clock, cfg.paper)
    book = PositionBook(cfg.charges)
    oms = OrderManager(broker, OrderRateLimiter(10, 250, 1000, clock.time), book, clock, cfg)
    return clock, market, st, broker, oms, book


def tick(market, clock, ltp, bid=None, ask=None, broker=None):
    t = Tick(4, 1, 2885, ltp, 0)
    if bid is not None:
        t.bids = ((bid, 100, 1),) * 5
        t.asks = ((ask, 100, 1),) * 5
        t.code = 8
    st = market.on_tick(t, clock.time(), clock.time())
    if broker is not None:
        broker.on_tick(st)
    return st


async def test_paper_limit_fills_only_when_marketable():
    clock, market, st, broker, oms, book = paper_setup()
    tick(market, clock, 1450.0, 1449.9, 1450.1)
    o = oms.create(INST, Side.BUY, 5, price_paise=144990)
    oms.submit(o)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert o.status is OrderStatus.SENT or o.status is OrderStatus.OPEN
    clock.advance(0.05)  # ack arrives, ask 1450.1 > limit 1449.9 -> no fill
    assert o.status is OrderStatus.OPEN and o.filled_qty == 0
    tick(market, clock, 1449.8, 1449.7, 1449.9, broker)
    assert o.status is OrderStatus.FILLED and o.avg_fill_paise == pytest.approx(144990)
    assert book.qty(INST) == 5


async def test_paper_market_with_slippage_and_ltp_only():
    clock, market, st, broker, oms, book = paper_setup(slippage=2)
    tick(market, clock, 1450.0)  # no book: LTP-based fills
    o = oms.create(INST, Side.SELL, 3, order_type=OrderType.MARKET)
    oms.submit(o)
    await asyncio.sleep(0)
    clock.advance(0.05)
    assert o.status is OrderStatus.FILLED and o.avg_fill_paise == pytest.approx(145000 - 20)


async def test_paper_stop_loss_triggers():
    clock, market, st, broker, oms, book = paper_setup()
    tick(market, clock, 1450.0, 1449.9, 1450.1)
    o = oms.create(INST, Side.SELL, 2, order_type=OrderType.STOP_LOSS_MARKET, trigger_paise=144000,
                   intent=Intent.EXIT)
    oms.submit(o)
    await asyncio.sleep(0)
    clock.advance(0.05)
    assert o.status is OrderStatus.OPEN
    tick(market, clock, 1440.5, 1440.4, 1440.6, broker)
    assert o.status is OrderStatus.OPEN
    tick(market, clock, 1439.9, 1439.8, 1440.0, broker)
    assert o.status is OrderStatus.FILLED and o.avg_fill_paise == pytest.approx(143980)


async def test_paper_super_order_target_and_oco():
    clock, market, st, broker, oms, book = paper_setup()
    tick(market, clock, 1450.0, 1449.9, 1450.1)
    o = oms.create(INST, Side.BUY, 4, price_paise=145100, kind=OrderKind.SUPER, target_paise=146000,
                   sl_paise=144000)
    oms.submit(o)
    await asyncio.sleep(0)
    clock.advance(0.05)
    assert o.status is OrderStatus.FILLED and book.qty(INST) == 4
    assert oms.super_exit_legs_working(o)
    tick(market, clock, 1460.1, 1460.0, 1460.2, broker)
    assert book.qty(INST) == 0 and o.exit_filled_qty == 4
    assert not oms.super_exit_legs_working(o)  # stop leg cancelled (OCO)
    assert book.get(INST, o.product).realised_paise == pytest.approx(4 * (146000 - 145010))


async def test_paper_super_trailing_stop():
    clock, market, st, broker, oms, book = paper_setup()
    tick(market, clock, 1450.0, 1449.9, 1450.1)
    o = oms.create(INST, Side.BUY, 1, price_paise=145100, kind=OrderKind.SUPER, sl_paise=144000, trail_paise=200)
    oms.submit(o)
    await asyncio.sleep(0)
    clock.advance(0.05)
    for px in (1452.0, 1455.0, 1458.0):
        tick(market, clock, px, px - 0.1, px + 0.1, broker)
    stop_leg = [x for x in broker.orders[o.broker_id] if x.leg == "STOP_LOSS_LEG"][0]
    assert stop_leg.trigger > 144000  # trailed up
    tick(market, clock, stop_leg.trigger / 100 - 0.1, stop_leg.trigger / 100 - 0.2, stop_leg.trigger / 100, broker)
    assert book.qty(INST) == 0


async def test_paper_cancel():
    clock, market, st, broker, oms, book = paper_setup()
    tick(market, clock, 1450.0, 1449.9, 1450.1)
    o = oms.create(INST, Side.BUY, 1, price_paise=140000)
    oms.submit(o)
    await asyncio.sleep(0)
    clock.advance(0.05)
    fut = oms.cancel(o)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    clock.advance(0.05)
    assert await fut and o.status is OrderStatus.CANCELLED


# ---------------------------------------------------------------------------------------------
# live broker against the mock
# ---------------------------------------------------------------------------------------------

CID = "1000000001"


@pytest.fixture
async def live():
    mock = MockDhan(client_id=CID, auto_ticks=False)
    ep = await mock.start()
    cfg = load_config(data={"execution": {"entry_ttl_ms": 0}}, env={"DHAN_CLIENT_ID": CID})
    rest = DhanRest(ep, CID, mock.token, order_timeout_s=0.4)
    await rest.start()
    broker = LiveBroker(rest, cfg.execution)
    book = PositionBook(cfg.charges)
    from dhanbot.clock import RealClock

    inst = Instrument(2885, "NSE_EQ", 1, "RELIANCE", tick_paise=10)
    oms = OrderManager(broker, OrderRateLimiter(10, 250, 1000), book, RealClock(), cfg,
                       resolve_instrument=lambda seg, sid: inst if sid == 2885 else None)
    ou = OrderUpdateConnection(ep.order_ws_url, CID, lambda: mock.token, oms.on_broker_update)
    task = asyncio.create_task(ou.run())
    await wait_for(lambda: ou.connected and mock._order_clients)
    yield mock, rest, broker, oms, book, inst
    await ou.stop()
    await asyncio.wait_for(task, 3)
    await oms.close()
    await rest.close()
    await mock.stop()


async def test_live_place_fill_via_order_ws(live):
    mock, rest, broker, oms, book, inst = live
    ask = mock.instrument("RELIANCE").ask
    o = oms.create(inst, Side.BUY, 2, price_paise=int(round(ask * 100)) + 50)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.FILLED)
    assert book.qty(inst) == 2 and o.avg_fill_paise == pytest.approx(ask * 100)
    body = mock.requests[-1][2] if mock.requests[-1][1] == "/v2/orders" else \
        [r for r in mock.requests if r[1] == "/v2/orders"][-1][2]
    assert body == {"dhanClientId": CID, "exchangeSegment": "NSE_EQ", "securityId": "2885", "productType": "INTRADAY",
                    "validity": "DAY", "disclosedQuantity": 0, "afterMarketOrder": False, "transactionType": "BUY",
                    "orderType": "LIMIT", "quantity": 2, "price": round(o.price_paise / 100, 2), "triggerPrice": 0.0,
                    "correlationId": o.cid}


async def test_live_timeout_resolved_by_correlation_id(live):
    mock, rest, broker, oms, book, inst = live
    mock.faults.place_timeout_once = True
    mock.faults.timeout_delay_s = 1.0
    ask = mock.instrument("RELIANCE").ask
    o = oms.create(inst, Side.BUY, 1, price_paise=int(round(ask * 100)) + 50)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.FILLED, timeout=4)
    assert sum(1 for r in mock.requests if r[1] == "/v2/orders") == 1  # never re-sent
    assert book.qty(inst) == 1


async def test_live_lost_order_marked_failed(live):
    mock, rest, broker, oms, book, inst = live
    mock.faults.place_lost_once = True
    mock.faults.timeout_delay_s = 1.0
    o = oms.create(inst, Side.BUY, 1, price_paise=140000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.FAILED, timeout=5)
    assert book.qty(inst) == 0


async def test_live_super_order_and_leg_cancel(live):
    mock, rest, broker, oms, book, inst = live
    i = mock.instrument("RELIANCE")
    entry = int(round(i.ask * 100)) + 50
    o = oms.create(inst, Side.BUY, 3, price_paise=entry, kind=OrderKind.SUPER, target_paise=entry + 2000,
                   sl_paise=entry - 2000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.FILLED and len(o.meta.get("leg_status", {})) == 2)
    super_body = [r for r in mock.requests if r[1] == "/v2/super/orders"][-1][2]
    assert super_body["targetPrice"] == round((entry + 2000) / 100, 2) and "validity" not in super_body
    assert book.qty(inst) == 3
    for leg in ("TARGET_LEG", "STOP_LOSS_LEG"):
        assert await oms.cancel(o, leg)
    assert await wait_for(lambda: not oms.super_exit_legs_working(o))
    mock.set_price("RELIANCE", (entry - 3000) / 100)  # would have hit the stop
    await asyncio.sleep(0.05)
    assert book.qty(inst) == 3


async def test_live_stop_leg_hit(live):
    mock, rest, broker, oms, book, inst = live
    i = mock.instrument("RELIANCE")
    entry = int(round(i.ask * 100)) + 50
    o = oms.create(inst, Side.BUY, 1, price_paise=entry, kind=OrderKind.SUPER, target_paise=entry + 5000,
                   sl_paise=entry - 1000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.FILLED)
    mock.set_price("RELIANCE", (entry - 1500) / 100)
    assert await wait_for(lambda: book.qty(inst) == 0 and o.exit_filled_qty == 1)


async def test_live_modify_and_cancel(live):
    mock, rest, broker, oms, book, inst = live
    o = oms.create(inst, Side.BUY, 1, price_paise=140000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.OPEN and o.broker_id)
    assert await oms.modify(o, price_paise=140100)
    assert mock.orders[o.broker_id][0].price == 1401.0 and o.price_paise == 140100
    assert await oms.cancel(o)
    assert await wait_for(lambda: o.status is OrderStatus.CANCELLED)


async def test_live_exchange_reject(live):
    mock, rest, broker, oms, book, inst = live
    rejects = []
    oms.reject_listeners.append(rejects.append)
    mock.faults.exchange_reject_next = "RMS: Margin exceeds"
    o = oms.create(inst, Side.BUY, 1, price_paise=140000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.REJECTED)
    assert "Margin" in o.reason and rejects == [o]


def test_body_for_market_order_has_zero_price():
    cfg = load_config(data={}, env={})
    b = LiveBroker(DhanRest.__new__(DhanRest), cfg.execution)
    b.rest.client_id = "1"
    from dhanbot.models import Order

    o = Order(1, INST, Side.SELL, 5, order_type=OrderType.MARKET, price_paise=12345, cid="C1")
    body = orjson.loads(b.build_body(o))
    assert body["price"] == 0.0 and body["orderType"] == "MARKET" and body["transactionType"] == "SELL"
