"""Order manager state machine, idempotency and rate gating (scripted fake broker)."""

from __future__ import annotations

import asyncio

import pytest

from dhanbot.broker.base import Broker, BrokerResult, LookupFailed, PlaceResult
from dhanbot.clock import RealClock
from dhanbot.config import load_config
from dhanbot.errors import Category, DhanError, network_error
from dhanbot.models import (LEG_STOP, LEG_TARGET, BrokerUpdate, Instrument, Intent, OrderKind, OrderStatus,
                            Side)
from dhanbot.oms import OrderManager, base36
from dhanbot.positions import PositionBook
from dhanbot.ratelimit import OrderRateLimiter

from .helpers import wait_for

INST = Instrument(2885, "NSE_EQ", 1, "RELIANCE", tick_paise=10)


class FakeBroker(Broker):
    def __init__(self) -> None:
        super().__init__()
        self.placed = []
        self.cancelled = []
        self.modified = []
        self.next_results: list = []  # PlaceResult or ("sleep", s, PlaceResult)
        self.lookup_result: object = None
        self.lookups = 0
        self._n = 0

    async def place(self, order):
        self.placed.append(order)
        if self.next_results:
            r = self.next_results.pop(0)
            if isinstance(r, tuple):
                await asyncio.sleep(r[1])
                r = r[2]
            return r
        self._n += 1
        return PlaceResult(True, f"B{self._n}", OrderStatus.OPEN)

    async def modify(self, order, *, price_paise=None, qty=None, trigger_paise=None, leg=None):
        self.modified.append((order, price_paise, leg))
        return BrokerResult(True)

    async def cancel(self, order, leg=None):
        self.cancelled.append((order, leg))
        return BrokerResult(True)

    async def lookup(self, cid):
        self.lookups += 1
        r = self.lookup_result
        if isinstance(r, Exception):
            raise r
        return r


def make(cfg_data=None, per_sec=10):
    cfg = load_config(data=cfg_data or {}, env={})
    broker = FakeBroker()
    book = PositionBook(cfg.charges)
    oms = OrderManager(broker, OrderRateLimiter(per_sec, 250, 1000), book, RealClock(), cfg, seq_start=100,
                       resolve_instrument=lambda seg, sid: INST if sid == 2885 else None)
    events = []
    oms.listeners.append(lambda e: events.append((e.kind, e.order.local_id, e.fill.qty if e.fill else None)))
    return oms, broker, book, events


def upd(o, **kw) -> BrokerUpdate:
    base = dict(broker_id=o.broker_id, cid=o.cid)
    base.update(kw)
    return BrokerUpdate(**base)


def test_base36_and_cid_format():
    assert base36(0, 3) == "000" and base36(35, 2) == "0Z" and base36(36, 2) == "10"


async def test_happy_path_ack_partial_fill_full_fill():
    oms, broker, book, events = make()
    o = oms.create(INST, Side.BUY, 10, price_paise=145000)
    assert oms.submit(o)
    assert o.cid.startswith("DB") and len(o.cid) <= 20
    assert await wait_for(lambda: o.status is OrderStatus.OPEN)
    assert ("ack", o.local_id, None) in events and o.broker_id == "B1"
    oms.on_broker_update(upd(o, status=OrderStatus.PART_FILLED, filled_qty=4, avg_price=1450.0))
    assert o.status is OrderStatus.PART_FILLED and book.qty(INST) == 4
    oms.on_broker_update(upd(o, status=OrderStatus.PART_FILLED, filled_qty=4, avg_price=1450.0))  # duplicate
    assert book.qty(INST) == 4
    oms.on_broker_update(upd(o, status=OrderStatus.FILLED, filled_qty=10, avg_price=1451.2))
    assert o.status is OrderStatus.FILLED and book.qty(INST) == 10
    fills = [e for e in events if e[0] == "fill"]
    assert [f[2] for f in fills] == [4, 6]
    assert o.avg_fill_paise == pytest.approx(145120)
    assert book.get(INST, o.product).avg_paise == pytest.approx(145120)
    # a late OPEN update must not move it backwards
    oms.on_broker_update(upd(o, status=OrderStatus.OPEN, filled_qty=10))
    assert o.status is OrderStatus.FILLED


async def test_ws_update_before_rest_response_matches_by_cid():
    oms, broker, book, events = make()
    broker.next_results = [("sleep", 0.1, PlaceResult(True, "B9", OrderStatus.OPEN))]
    o = oms.create(INST, Side.BUY, 1, price_paise=145000)
    oms.submit(o)
    await asyncio.sleep(0.01)
    oms.on_broker_update(BrokerUpdate(broker_id="B9", cid=o.cid, status=OrderStatus.FILLED, filled_qty=1,
                                      avg_price=1450.0))
    assert o.status is OrderStatus.FILLED and o.broker_id == "B9"
    await asyncio.sleep(0.15)
    assert o.status is OrderStatus.FILLED  # REST "OPEN" arriving later is ignored


async def test_broker_reject_notifies_listeners():
    oms, broker, book, events = make()
    rejects = []
    oms.reject_listeners.append(rejects.append)
    broker.next_results = [PlaceResult(False, error=DhanError(code="DH-906", message="Insufficient margin",
                                                              category=Category.MARGIN))]
    o = oms.create(INST, Side.BUY, 1, price_paise=145000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.REJECTED)
    assert "Insufficient margin" in o.reason and rejects == [o]


async def test_timeout_then_found_by_cid_is_adopted_not_resent():
    oms, broker, book, events = make()
    broker.next_results = [PlaceResult(False, error=network_error("timeout", True))]
    o = oms.create(INST, Side.BUY, 1, price_paise=145000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.UNKNOWN)
    broker.lookup_result = BrokerUpdate(broker_id="B77", cid=o.cid, status=OrderStatus.FILLED, filled_qty=1,
                                        avg_price=1450.0)
    assert await wait_for(lambda: o.status is OrderStatus.FILLED, timeout=3)
    assert len(broker.placed) == 1 and book.qty(INST) == 1 and o.broker_id == "B77"


async def test_timeout_not_found_entry_fails_without_resend():
    oms, broker, book, events = make()
    broker.next_results = [PlaceResult(False, error=network_error("timeout", True))]
    broker.lookup_result = None
    o = oms.create(INST, Side.BUY, 1, price_paise=145000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.FAILED, timeout=4)
    assert len(broker.placed) == 1 and broker.lookups >= 1


async def test_timeout_not_found_exit_is_resent_once():
    oms, broker, book, events = make()
    broker.next_results = [PlaceResult(False, error=network_error("timeout", True))]
    broker.lookup_result = None
    o = oms.create(INST, Side.SELL, 1, price_paise=145000, intent=Intent.EXIT, role="exit")
    oms.submit(o)
    assert await wait_for(lambda: len(broker.placed) == 2, timeout=4)
    clone = broker.placed[1]
    assert clone.resent and clone.cid != o.cid and o.meta["resent_as"] == clone.local_id


async def test_lookup_failures_keep_trying():
    oms, broker, book, events = make()
    broker.next_results = [PlaceResult(False, error=network_error("timeout", True))]
    broker.lookup_result = LookupFailed("down")
    o = oms.create(INST, Side.BUY, 1, price_paise=145000)
    oms.submit(o)
    assert await wait_for(lambda: o.status is OrderStatus.FAILED, timeout=4)
    assert broker.lookups == 4


async def test_rate_limit_rejects_entries_but_queues_exits():
    oms, broker, book, events = make(per_sec=2)
    entries = [oms.create(INST, Side.BUY, 1, price_paise=145000) for _ in range(3)]
    results = [oms.submit(o) for o in entries]
    assert results == [True, True, False]
    assert entries[2].status is OrderStatus.REJECTED and "rate limit" in entries[2].reason
    ex = oms.create(INST, Side.SELL, 1, price_paise=145000, intent=Intent.EXIT, role="exit")
    assert oms.submit(ex) and ex.status is OrderStatus.QUEUED
    assert await wait_for(lambda: ex.status is OrderStatus.OPEN, timeout=2)


async def test_broker_429_requeues_exit():
    oms, broker, book, events = make()
    broker.next_results = [PlaceResult(False, error=DhanError(code="DH-904", category=Category.RATE_LIMIT))]
    ex = oms.create(INST, Side.SELL, 1, price_paise=145000, intent=Intent.EXIT, role="exit")
    oms.submit(ex)
    assert await wait_for(lambda: ex.status is OrderStatus.OPEN, timeout=3)
    assert len(broker.placed) == 2


async def test_entry_ttl_reprices_once_then_cancels():
    oms, broker, book, events = make({"execution": {"entry_ttl_ms": 100}})
    oms.reprice_fn = lambda o: o.price_paise + 10
    o = oms.create(INST, Side.BUY, 1, price_paise=145000, role="entry")
    oms.submit(o)
    assert await wait_for(lambda: len(broker.modified) == 1, timeout=2)
    assert broker.modified[0][1] == 145010 and o.repriced
    assert await wait_for(lambda: len(broker.cancelled) == 1, timeout=2)


async def test_modify_cap_and_cancel_before_bind():
    oms, broker, book, events = make()
    o = oms.create(INST, Side.BUY, 1, price_paise=145000)
    oms.submit(o)
    await wait_for(lambda: o.status is OrderStatus.OPEN)
    o.modify_count = 25
    assert (await oms.modify(o, price_paise=1)) is False
    q = oms.create(INST, Side.BUY, 1, price_paise=145000)
    q.status = OrderStatus.NEW
    q2 = oms.create(INST, Side.BUY, 1, price_paise=145000)
    q2.cid = "X"
    q2.status = OrderStatus.QUEUED
    assert await oms.cancel(q2) is True and q2.status is OrderStatus.CANCELLED


async def test_super_order_exit_leg_fills_are_opposite_side():
    oms, broker, book, events = make()
    o = oms.create(INST, Side.BUY, 10, price_paise=145000, kind=OrderKind.SUPER, target_paise=146000,
                   sl_paise=144000)
    oms.submit(o)
    await wait_for(lambda: o.status is OrderStatus.OPEN)
    oms.on_broker_update(upd(o, status=OrderStatus.FILLED, filled_qty=10, avg_price=1450.0, leg="ENTRY_LEG"))
    assert book.qty(INST) == 10 and o.status is OrderStatus.FILLED
    assert oms.super_exit_legs_working(o)
    oms.on_broker_update(upd(o, status=OrderStatus.OPEN, filled_qty=0, leg=LEG_TARGET, side=Side.SELL))
    oms.on_broker_update(upd(o, status=OrderStatus.OPEN, filled_qty=0, leg=LEG_STOP, side=Side.SELL))
    oms.on_broker_update(upd(o, status=OrderStatus.FILLED, filled_qty=10, avg_price=1440.0, leg=LEG_STOP,
                             side=Side.SELL))
    oms.on_broker_update(upd(o, status=OrderStatus.CANCELLED, filled_qty=0, leg=LEG_TARGET, side=Side.SELL))
    assert book.qty(INST) == 0 and o.exit_filled_qty == 10
    assert not oms.super_exit_legs_working(o)
    pos = book.get(INST, o.product)
    assert pos.realised_paise == pytest.approx(10 * -1000)


async def test_unknown_bot_order_is_adopted_and_manual_ignored():
    oms, broker, book, events = make()
    unmatched = []
    oms.unmatched_listeners.append(unmatched.append)
    oms.on_broker_update(BrokerUpdate(broker_id="M1", cid="", status=OrderStatus.FILLED, filled_qty=1))
    assert len(unmatched) == 1
    oms.ingest_snapshot(BrokerUpdate(broker_id="Z1", cid="DB261104000ZZZ", status=OrderStatus.FILLED, filled_qty=3,
                                     qty=3, avg_price=1450.0, side=Side.BUY, security_id=2885, segment="NSE_EQ",
                                     product="INTRADAY"))
    adopted = oms.by_broker["Z1"]
    assert adopted.role == "adopted" and adopted.status is OrderStatus.FILLED and adopted.filled_qty == 3
    assert book.qty(INST) == 0  # snapshot does not create fills; positions come from the broker
