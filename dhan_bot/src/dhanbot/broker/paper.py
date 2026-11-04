"""Paper broker: simulated execution on the live (or replayed) market data.

Behaves like the live broker from the OMS's point of view: ``place`` returns an order id at once,
acks / fills arrive later through ``on_update`` as ``BrokerUpdate`` objects with the same fields the
order-update WebSocket produces.

Fill model
* LIMIT BUY fills when best ask <= limit (or LTP <= limit without a book) at
  ``min(limit, ask + slippage)``; SELL mirrors it.
* MARKET fills at best ask / bid (LTP without a book) +/- slippage ticks.
* STOP_LOSS / STOP_LOSS_MARKET trigger on LTP, then behave as LIMIT / MARKET.
* Super orders: after the entry fills, a target LIMIT and a stop SL-M leg are created (OCO), with
  optional trailing of the stop.
* Simulated acknowledgement latency (``paper.ack_ms``) uses the engine clock, so replays stay
  deterministic.
"""

from __future__ import annotations

import itertools
import logging

from ..clock import RealClock
from ..config import PaperCfg
from ..market_state import InstrumentState, MarketState
from ..models import (LEG_ENTRY, LEG_STOP, LEG_TARGET, BrokerUpdate, Order, OrderKind, OrderStatus, OrderType,
                      Side)
from .base import Broker, BrokerResult, PlaceResult

log = logging.getLogger(__name__)


class _PO:
    __slots__ = ("bid", "cid", "inst", "side", "qty", "otype", "price", "trigger", "product", "status", "filled",
                 "avg", "last", "triggered", "leg", "kind", "target", "stop", "trail", "best", "entry_avg", "reason")

    def __init__(self, bid: str, o: Order, leg: str, side: Side, otype: OrderType, price: int, trigger: int) -> None:
        self.bid, self.cid, self.inst, self.side, self.qty = bid, o.cid, o.instrument, side, o.qty
        self.otype, self.price, self.trigger, self.product = otype, price, trigger, o.product
        self.status = OrderStatus.OPEN
        self.filled = 0
        self.avg = 0.0
        self.last = 0.0
        self.triggered = False
        self.leg = leg
        self.kind = o.kind
        self.target, self.stop, self.trail = o.target_paise, o.sl_paise, o.trail_paise
        self.best = 0.0
        self.entry_avg = 0.0
        self.reason = ""

    @property
    def working(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PART_FILLED)


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, market: MarketState, clock: RealClock, cfg: PaperCfg) -> None:
        super().__init__()
        self.market = market
        self.clock = clock
        self.cfg = cfg
        self._ids = itertools.count(90000000001)
        self.orders: dict[str, list[_PO]] = {}  # broker id -> legs (entry first)
        self._by_cid: dict[str, str] = {}
        self._working: dict[tuple[int, int], list[_PO]] = {}
        self.ack_s = max(0.0, cfg.ack_ms / 1000.0)

    # ---- interface ---------------------------------------------------------------------------
    async def place(self, order: Order) -> PlaceResult:
        bid = str(next(self._ids))
        leg = LEG_ENTRY if order.kind is OrderKind.SUPER else ""
        po = _PO(bid, order, leg, order.side, order.order_type, order.price_paise, order.trigger_paise)
        self.orders[bid] = [po]
        if order.cid:
            self._by_cid[order.cid] = bid
        self.clock.call_later(self.ack_s, self._ack, po)
        return PlaceResult(True, bid, OrderStatus.OPEN)

    async def modify(self, order: Order, *, price_paise: int | None = None, qty: int | None = None,
                     trigger_paise: int | None = None, leg: str | None = None) -> BrokerResult:
        legs = self.orders.get(order.broker_id)
        if not legs:
            return BrokerResult(False)
        po = next((x for x in legs if x.leg == (leg or legs[0].leg)), legs[0])
        if not po.working:
            return BrokerResult(False)

        def apply() -> None:
            if price_paise is not None:
                po.price = price_paise
            if trigger_paise is not None:
                po.trigger = trigger_paise
            if qty:
                po.qty = qty
            self._emit(po)
            self._try_fill(po)

        self.clock.call_later(self.ack_s / 2, apply)
        return BrokerResult(True)

    async def cancel(self, order: Order, leg: str | None = None) -> BrokerResult:
        legs = self.orders.get(order.broker_id)
        if not legs:
            return BrokerResult(False)
        targets = legs if (leg in (None, LEG_ENTRY)) else [x for x in legs if x.leg == leg]
        if not any(x.working for x in targets):
            return BrokerResult(False)
        if leg not in (None, LEG_ENTRY):  # a cancelled exit leg must not be (re)created later
            if leg == LEG_TARGET:
                legs[0].target = 0
            elif leg == LEG_STOP:
                legs[0].stop = 0

        def apply() -> None:
            for x in targets:
                if x.working:
                    x.status = OrderStatus.CANCELLED
                    self._unwork(x)
                    self._emit(x)

        self.clock.call_later(self.ack_s / 2, apply)
        return BrokerResult(True)

    async def lookup(self, cid: str) -> BrokerUpdate | None:
        bid = self._by_cid.get(cid)
        return self._update(self.orders[bid][0]) if bid else None

    # ---- simulation ----------------------------------------------------------------------------
    def _ack(self, po: _PO) -> None:
        if po.status is not OrderStatus.OPEN:
            return
        self._emit(po)
        self._work(po)
        self._try_fill(po)

    def _work(self, po: _PO) -> None:
        lst = self._working.setdefault(po.inst.key, [])
        if po not in lst:
            lst.append(po)

    def _unwork(self, po: _PO) -> None:
        lst = self._working.get(po.inst.key)
        if lst and po in lst:
            lst.remove(po)

    def has_working(self, key: tuple[int, int]) -> bool:
        return bool(self._working.get(key))

    def on_tick(self, st: InstrumentState) -> None:
        """Engine calls this for every tick of an instrument that has working paper orders."""
        lst = self._working.get(st.inst.key)
        if not lst:
            return
        for po in list(lst):
            if po.kind is OrderKind.SUPER and po.leg == LEG_STOP and po.trail:
                self._trail(po, st)
            self._try_fill(po, st)

    def _trail(self, po: _PO, st: InstrumentState) -> None:
        px = st.ltp * 100
        entry = self.orders[po.bid][0]
        dist = abs(entry.avg - entry.stop) if entry.stop else abs(entry.avg - po.trigger)
        if po.side is Side.SELL:  # protecting a long
            po.best = max(po.best or entry.avg, px)
            new = po.inst.round_to_tick(po.best - dist, "down")
            if new >= po.trigger + po.trail:
                po.trigger = new
        else:
            po.best = min(po.best or entry.avg, px)
            new = po.inst.round_to_tick(po.best + dist, "up")
            if new <= po.trigger - po.trail:
                po.trigger = new

    def _try_fill(self, po: _PO, st: InstrumentState | None = None) -> None:
        if not po.working:
            return
        st = st or self.market.of(po.inst)
        if st is None or st.ltp <= 0:
            return
        tick = po.inst.tick_paise
        slip = self.cfg.slippage_ticks * tick
        ltp = st.ltp * 100
        otype = po.otype
        if otype in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET):
            if not po.triggered:
                hit = ltp >= po.trigger if po.side is Side.BUY else ltp <= po.trigger
                if not hit:
                    return
                po.triggered = True
            otype = OrderType.MARKET if otype is OrderType.STOP_LOSS_MARKET else OrderType.LIMIT
        ask = st.ask * 100 if st.ask > 0 else ltp
        bid = st.bid * 100 if st.bid > 0 else ltp
        if otype is OrderType.MARKET:
            px = ask + slip if po.side is Side.BUY else max(tick, bid - slip)
        elif po.side is Side.BUY:
            if ask > po.price:
                return
            px = min(po.price, ask + slip)
        else:
            if bid < po.price:
                return
            px = max(po.price, bid - slip)
        qty = po.qty - po.filled
        if self.cfg.partial_fills:
            avail = st.ask_qty if po.side is Side.BUY else st.bid_qty
            if avail > 0:
                lot = po.inst.lot_size
                qty = max(lot, min(qty, (avail // lot) * lot))
        self._fill(po, qty, px)

    def _fill(self, po: _PO, qty: int, px: float) -> None:
        po.avg = (po.avg * po.filled + px * qty) / (po.filled + qty)
        po.filled += qty
        po.last = px
        po.status = OrderStatus.FILLED if po.filled >= po.qty else OrderStatus.PART_FILLED
        if po.status is OrderStatus.FILLED:
            self._unwork(po)
        self._emit(po)
        legs = self.orders[po.bid]
        if po.kind is OrderKind.SUPER and po.leg == LEG_ENTRY and po.status is OrderStatus.FILLED:
            self._spawn_legs(po)
        elif po.leg in (LEG_TARGET, LEG_STOP) and po.status is OrderStatus.FILLED:
            for other in legs[1:]:
                if other is not po and other.working:
                    other.status = OrderStatus.CANCELLED
                    self._unwork(other)
                    self._emit(other)

    def _spawn_legs(self, entry: _PO) -> None:
        legs = self.orders[entry.bid]
        exit_side = entry.side.opposite
        fake = _FakeOrder(entry)
        if entry.target:
            t = _PO(entry.bid, fake, LEG_TARGET, exit_side, OrderType.LIMIT, entry.target, 0)  # type: ignore[arg-type]
            legs.append(t)
        if entry.stop:
            s = _PO(entry.bid, fake, LEG_STOP, exit_side, OrderType.STOP_LOSS_MARKET, 0, entry.stop)  # type: ignore[arg-type]
            s.best = entry.avg
            legs.append(s)
        for leg in legs[1:]:
            self._emit(leg)
            self._work(leg)
        for leg in legs[1:]:
            self._try_fill(leg)

    # ---- updates -------------------------------------------------------------------------------
    def _update(self, po: _PO) -> BrokerUpdate:
        return BrokerUpdate(broker_id=po.bid, cid=po.cid, status=po.status, raw_status=po.status.value,
                            filled_qty=po.filled, avg_price=round(po.avg / 100, 4) if po.filled else 0.0,
                            last_price=round(po.last / 100, 4), remaining=po.qty - po.filled, qty=po.qty,
                            price=po.price / 100, trigger=po.trigger / 100, reason=po.reason,
                            leg=po.leg or None, side=po.side, security_id=po.inst.security_id,
                            segment=po.inst.segment, product=po.product.value)

    def _emit(self, po: _PO) -> None:
        try:
            self.on_update(self._update(po))
        except Exception:
            log.exception("paper broker update handler raised")


class _FakeOrder:
    """Adapter so exit legs can reuse ``_PO.__init__``."""

    def __init__(self, entry: _PO) -> None:
        self.cid = entry.cid
        self.instrument = entry.inst
        self.qty = entry.qty
        self.product = entry.product
        self.kind = OrderKind.SUPER
        self.target_paise = entry.target
        self.sl_paise = entry.stop
        self.trail_paise = entry.trail
