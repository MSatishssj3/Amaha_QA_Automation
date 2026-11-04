"""Order manager: lifecycle state machine, rate-gating, idempotency, fills -> positions.

State machine (see README):

    NEW -> QUEUED -> SENT -> OPEN -> PART_FILLED -> FILLED
                      |       \\-> CANCELLED / EXPIRED / REJECTED
                      |-> REJECTED (broker said no)
                      \\-> UNKNOWN (timeout / dropped connection) -> resolved by correlationId -> real state
                                                                  \\-> FAILED (confirmed not at broker)

Rules
* Transitions are monotonic: a late or duplicated update can never move an order backwards.
  The only exception is broker truth overriding our own guesses (UNKNOWN / FAILED).
* No blind retries. An order whose placement outcome is unknown is looked up by correlationId.
  Entries are never re-sent automatically; exits are re-sent once after a confirmed "not found".
* Order API calls (place / modify / cancel) pass the rate limiter. Exits and cancels queue with
  priority; entries are rejected when no token is available (optionally wait a few ms).
* Fills are derived from cumulative filled quantity per leg, so duplicate updates are harmless.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from .broker.base import Broker, LookupFailed, PlaceResult
from .clock import RealClock
from .config import AppConfig
from .errors import Category, network_error
from .models import (LEG_ENTRY, LEG_STOP, LEG_TARGET, BrokerUpdate, Fill, Instrument, Intent, Order, OrderEvent,
                     OrderKind, OrderStatus, OrderType, Product, Side)
from .positions import ClosedTrade, PositionBook
from .ratelimit import OrderRateLimiter

log = logging.getLogger(__name__)

MAX_MODIFICATIONS = 25
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_EVENT_FOR = {
    OrderStatus.OPEN: "ack",
    OrderStatus.FILLED: "filled",  # completely filled (individual "fill" events precede it)
    OrderStatus.CANCELLED: "cancelled",
    OrderStatus.REJECTED: "rejected",
    OrderStatus.EXPIRED: "expired",
    OrderStatus.FAILED: "failed",
    OrderStatus.UNKNOWN: "unknown",
}
_MAIN = "MAIN"


def base36(n: int, width: int) -> str:
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out.rjust(width, "0")[-width:]


class OrderManager:
    def __init__(self, broker: Broker, limiter: OrderRateLimiter, positions: PositionBook, clock: RealClock,
                 cfg: AppConfig, *, seq_start: int = 0,
                 resolve_instrument: Callable[[str | None, int | None], Instrument | None] | None = None,
                 latency=None) -> None:
        self.broker = broker
        broker.on_update = self.on_broker_update
        self.limiter = limiter
        self.positions = positions
        self.clock = clock
        self.cfg = cfg
        self.prefix = cfg.dhan.cid_prefix
        self.resolve_instrument = resolve_instrument
        self.latency = latency
        self._ids = itertools.count(1)
        self._seq = seq_start or int(clock.time() % 86400) * 10  # unique across same-day restarts
        self.orders: dict[int, Order] = {}
        self.by_cid: dict[str, Order] = {}
        self.by_broker: dict[str, Order] = {}
        self.listeners: list[Callable[[OrderEvent], None]] = []
        self.trade_listeners: list[Callable[[ClosedTrade, Order], None]] = []
        self.reject_listeners: list[Callable[[Order], None]] = []
        self.unmatched_listeners: list[Callable[[BrokerUpdate], None]] = []
        self.reprice_fn: Callable[[Order], int] | None = None
        self._prio: deque[Callable[[], Awaitable[None]]] = deque()
        self._entries: deque[tuple[Callable[[], Awaitable[None]], float, Order]] = deque()
        self._drain_handle = None
        self._tasks: set[asyncio.Task] = set()
        self._waiters: list[tuple[Callable[[], bool], asyncio.Future]] = []
        self._ttl: dict[int, object] = {}
        self._warned_status: set[str] = set()
        self.placed_today = 0
        self._day_lo = self._day_hi = 0.0
        self._day_str = ""

    # ---- creation / submission -------------------------------------------------------------------
    def create(self, instrument: Instrument, side: Side, qty: int, **kw) -> Order:
        o = Order(next(self._ids), instrument, side, int(qty), **kw)
        o.created_ns = time.perf_counter_ns()
        self.orders[o.local_id] = o
        return o

    def next_cid(self) -> str:
        self._seq += 1
        now = self.clock.time()
        if not self._day_lo <= now < self._day_hi:  # recompute the date part once per IST day
            d = self.clock.now()
            self._day_str = f"{self.prefix}{d:%y%m%d}"
            self._day_lo = d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            self._day_hi = self._day_lo + 86400
        return self._day_str + base36(self._seq, 6)

    def submit(self, order: Order) -> bool:
        """Send an order that already passed risk checks. Returns False if rejected locally (rate limit)."""
        if order.status is not OrderStatus.NEW:
            raise ValueError(f"order {order.local_id} already submitted ({order.status})")
        if not order.cid:
            order.cid = self.next_cid()
        self.by_cid[order.cid] = order
        order.status = OrderStatus.QUEUED
        if self._gate(lambda: self._send(order), priority=order.intent is Intent.EXIT, order=order):
            self.placed_today += 1
            return True
        self.reject_local(order, "rate limit: no order slot available (Dhan allows 10/s, 250/min)")
        return False

    def reject_local(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        order.reason = reason
        order.done_ns = time.perf_counter_ns()
        order.meta["local_reject"] = True
        self._emit(OrderEvent("rejected", order))

    # ---- rate gate -------------------------------------------------------------------------------
    def _gate(self, action: Callable[[], Awaitable[None]], priority: bool, order: Order | None = None) -> bool:
        if not self._prio and self.limiter.try_acquire():
            self._spawn(action())
            return True
        if priority:
            self._prio.append(action)
            self._schedule_drain()
            return True
        wait_ms = self.cfg.rate_limits.entry_max_wait_ms
        if wait_ms > 0 and order is not None and self.limiter.wait_time() * 1000 <= wait_ms:
            self._entries.append((action, self.clock.time() + wait_ms / 1000.0, order))
            self._schedule_drain()
            return True
        return False

    def _schedule_drain(self) -> None:
        if self._drain_handle is not None:
            return
        delay = min(max(self.limiter.wait_time(), 0.001), 1.0)
        self._drain_handle = self.clock.call_later(delay, self._drain)

    def _drain(self) -> None:
        self._drain_handle = None
        while self._prio and self.limiter.try_acquire():
            self._spawn(self._prio.popleft()())
        now = self.clock.time()
        while not self._prio and self._entries:
            action, deadline, order = self._entries[0]
            if now > deadline:
                self._entries.popleft()
                self.reject_local(order, "rate limit: waited too long for an order slot")
                continue
            if not self.limiter.try_acquire():
                break
            self._entries.popleft()
            self._spawn(action())
        if self._prio or self._entries:
            self._schedule_drain()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("OMS task failed", exc_info=task.exception())

    @property
    def queued(self) -> int:
        return len(self._prio) + len(self._entries)

    # ---- placement ---------------------------------------------------------------------------------
    async def _send(self, order: Order) -> None:
        if order.status is not OrderStatus.QUEUED:
            return
        order.status = OrderStatus.SENT
        order.sent_ns = time.perf_counter_ns()
        t_frame = order.meta.get("t_frame")
        if t_frame and self.latency is not None:
            self.latency.record("tick_to_send", order.sent_ns - t_frame)
        try:
            res = await self.broker.place(order)
        except Exception as e:  # defensive: a broker bug must not lose the order state
            log.exception("broker.place raised")
            res = PlaceResult(False, error=network_error(str(e), timeout=True))
        order.ack_ns = time.perf_counter_ns()
        if self.latency is not None:
            self.latency.record("order_send_to_ack", order.ack_ns - order.sent_ns)
        if res.ok:
            if res.broker_id:
                self._bind(order, res.broker_id)
            if order.status in (OrderStatus.SENT, OrderStatus.UNKNOWN):
                self._transition(order, res.status or OrderStatus.OPEN)
            return
        err = res.error
        if err is not None and err.timeout:
            if order.status is OrderStatus.SENT:
                self._transition(order, OrderStatus.UNKNOWN, str(err))
                self._spawn(self._resolve_unknown(order))
            return
        if err is not None and err.category is Category.RATE_LIMIT:
            self.limiter.cooldown(1.0)
            if order.intent is Intent.EXIT and order.status is OrderStatus.SENT:
                order.status = OrderStatus.QUEUED  # exits are never dropped: retry after cooldown
                self._prio.append(lambda: self._send(order))
                self._schedule_drain()
                return
        if order.status is OrderStatus.SENT:
            order.meta["error_category"] = err.category.value if err else ""
            self._transition(order, OrderStatus.REJECTED, str(err) if err else "rejected")

    def _bind(self, order: Order, broker_id: str) -> None:
        if not order.broker_id:
            order.broker_id = broker_id
        self.by_broker[broker_id] = order
        if order.meta.pop("cancel_requested", False) and not order.is_done:
            self.cancel(order)

    async def _resolve_unknown(self, order: Order) -> None:
        for delay in (0.15, 0.25, 0.6, 1.0):
            await asyncio.sleep(delay)
            if order.status is not OrderStatus.UNKNOWN:
                return
            try:
                upd = await self.broker.lookup(order.cid)
            except LookupFailed as e:
                log.warning("lookup of %s failed (%s); retrying", order.cid, e)
                continue
            if upd is not None:
                log.info("order %s with unknown outcome found at broker: %s", order.cid, upd.raw_status)
                self.on_broker_update(upd)
                if order.status is OrderStatus.UNKNOWN:
                    self._transition(order, OrderStatus.OPEN)
                return
        if order.status is not OrderStatus.UNKNOWN:
            return
        self._transition(order, OrderStatus.FAILED, "placement timed out and the order is not at the broker")
        if order.intent is Intent.EXIT and not order.resent:
            clone = self.create(order.instrument, order.side, order.remaining or order.qty,
                                order_type=order.order_type, product=order.product, validity=order.validity,
                                intent=order.intent, kind=order.kind, price_paise=order.price_paise,
                                trigger_paise=order.trigger_paise, strategy=order.strategy, tag=order.tag,
                                role=order.role, parent_id=order.parent_id)
            clone.resent = True
            if self.reprice_fn is not None and clone.order_type is OrderType.LIMIT:
                clone.price_paise = self.reprice_fn(clone) or clone.price_paise
            order.meta["resent_as"] = clone.local_id
            log.warning("re-sending exit order %s as #%d", order.cid, clone.local_id)
            self.submit(clone)

    # ---- broker updates ----------------------------------------------------------------------------
    def find(self, upd: BrokerUpdate) -> Order | None:
        o = self.by_broker.get(upd.broker_id)
        if o is None and upd.cid:
            o = self.by_cid.get(upd.cid)
        return o

    def on_broker_update(self, upd: BrokerUpdate, apply_fills: bool = True) -> None:
        order = self.find(upd)
        if order is None:
            if upd.cid and upd.cid.startswith(self.prefix):
                order = self.adopt(upd)
                apply_fills = False
            if order is None:
                for fn in self.unmatched_listeners:
                    fn(upd)
                return
        if not order.broker_id or upd.broker_id not in self.by_broker:
            self._bind(order, upd.broker_id)
        if upd.status is None and upd.raw_status and upd.raw_status not in self._warned_status:
            self._warned_status.add(upd.raw_status)
            log.warning("unrecognised order status %r from broker (treated as no status change)", upd.raw_status)

        is_super = order.kind is OrderKind.SUPER
        leg = upd.leg
        is_exit_leg = is_super and (leg in (LEG_TARGET, LEG_STOP) or
                                    (leg is None and upd.side is not None and upd.side is not order.side))
        key = (leg or "EXIT") if is_exit_leg else _MAIN
        if upd.filled_qty is not None:
            prev_f, prev_avg = order.legs.get(key, (0, 0.0))
            new_f = upd.filled_qty
            if new_f > prev_f:
                delta = new_f - prev_f
                if upd.avg_price:
                    new_avg = upd.avg_price * 100.0
                    px = (new_avg * new_f - prev_avg * prev_f) / delta
                    if px <= 0:  # rounding noise on tiny deltas
                        px = new_avg
                elif upd.last_price:
                    px = upd.last_price * 100.0
                    new_avg = (prev_avg * prev_f + px * delta) / new_f
                else:
                    px = float(order.price_paise or order.trigger_paise)
                    new_avg = (prev_avg * prev_f + px * delta) / new_f
                order.legs[key] = (new_f, new_avg)
                side = order.side.opposite if is_exit_leg else order.side
                if is_exit_leg:
                    order.exit_filled_qty += delta
                else:
                    order.filled_qty = new_f
                    order.avg_fill_paise = new_avg
                if apply_fills:
                    fill = Fill(order.local_id, order.instrument, order.product, side, delta, px, key, self.clock.time())
                    trade = self.positions.apply_fill(fill, first_fill_of_order=prev_f == 0)
                    if self.latency is not None and not is_exit_leg and prev_f == 0 and order.sent_ns:
                        self.latency.record("order_send_to_fill", time.perf_counter_ns() - order.sent_ns)
                    self._emit(OrderEvent("fill", order, fill))
                    if trade is not None:
                        for fn in self.trade_listeners:
                            try:
                                fn(trade, order)
                            except Exception:
                                log.exception("trade listener failed")

        if is_exit_leg:
            legs = order.meta.setdefault("leg_status", {})
            if upd.status is not None:
                legs[leg or "EXIT"] = upd.status
                self._emit(OrderEvent("leg_update", order))
        else:
            new = upd.status
            if new is None or new is OrderStatus.OPEN:
                if order.filled_qty >= order.qty > 0:
                    new = OrderStatus.FILLED
                elif order.filled_qty > 0:
                    new = OrderStatus.PART_FILLED
            if new is OrderStatus.FILLED and order.filled_qty < order.qty and upd.filled_qty is not None:
                new = OrderStatus.PART_FILLED if order.filled_qty else OrderStatus.OPEN
            if new is not None:
                self._transition(order, new, upd.reason if new in (OrderStatus.REJECTED, OrderStatus.CANCELLED,
                                                                    OrderStatus.EXPIRED) else "")
        self._check_waiters()

    def adopt(self, upd: BrokerUpdate) -> Order | None:
        """Take ownership of a bot order we have no local record of (e.g. after a restart)."""
        inst = self.resolve_instrument(upd.segment, upd.security_id) if self.resolve_instrument else None
        if inst is None:
            log.warning("cannot adopt order %s (%s): unknown instrument", upd.broker_id, upd.cid)
            return None
        try:
            product = Product(upd.product) if upd.product else Product.INTRADAY
        except ValueError:
            product = Product.INTRADAY
        kind = OrderKind.SUPER if upd.leg in (LEG_ENTRY, LEG_TARGET, LEG_STOP) else OrderKind.REGULAR
        side = upd.side or Side.BUY
        if kind is OrderKind.SUPER and upd.leg in (LEG_TARGET, LEG_STOP):
            side = side.opposite
        o = self.create(inst, side, upd.qty or upd.filled_qty or 0, product=product, kind=kind, role="adopted",
                        price_paise=int(round((upd.price or 0) * 100)), cid=upd.cid, broker_id=upd.broker_id,
                        status=OrderStatus.OPEN)
        self.by_cid[o.cid] = o
        self.by_broker[o.broker_id] = o
        log.info("adopted bot order %s (%s %s %s)", o.cid, side.value, o.qty, inst.trading_symbol)
        return o

    def ingest_snapshot(self, upd: BrokerUpdate) -> None:
        """Startup reconciliation: record state without generating fills (positions come from the broker)."""
        self.on_broker_update(upd, apply_fills=False)

    # ---- transitions / events ------------------------------------------------------------------------
    def _transition(self, order: Order, new: OrderStatus, reason: str = "") -> bool:
        old = order.status
        if new is old:
            return False
        overridable = old in (OrderStatus.UNKNOWN, OrderStatus.FAILED)
        if old.is_terminal and not overridable:
            return False
        if new.rank < old.rank and not overridable:
            return False
        order.status = new
        if reason:
            order.reason = reason
        if new.is_terminal:
            order.done_ns = time.perf_counter_ns()
            h = self._ttl.pop(order.local_id, None)
            if h is not None:
                h.cancel()  # type: ignore[attr-defined]
        elif new is OrderStatus.OPEN and old in (OrderStatus.SENT, OrderStatus.UNKNOWN, OrderStatus.QUEUED):
            self._arm_ttl(order)
        kind = _EVENT_FOR.get(new)
        if kind:
            self._emit(OrderEvent(kind, order))
        if new is OrderStatus.REJECTED and not order.meta.get("local_reject"):
            for fn in self.reject_listeners:
                fn(order)
        self._check_waiters()
        return True

    def _emit(self, ev: OrderEvent) -> None:
        for fn in self.listeners:
            try:
                fn(ev)
            except Exception:
                log.exception("order event listener failed")

    # ---- entry time-to-live ----------------------------------------------------------------------------
    def _arm_ttl(self, order: Order) -> None:
        ttl = self.cfg.execution.entry_ttl_ms / 1000.0
        if (ttl <= 0 or order.role != "entry" or order.order_type is not OrderType.LIMIT
                or order.local_id in self._ttl):
            return
        self._ttl[order.local_id] = self.clock.call_later(ttl, self._ttl_check, order)

    def _ttl_check(self, order: Order) -> None:
        self._ttl.pop(order.local_id, None)
        if order.is_done or order.remaining == 0 or order.status is OrderStatus.UNKNOWN:
            return
        if self.cfg.execution.reprice_once and not order.repriced and self.reprice_fn is not None:
            new = self.reprice_fn(order)
            if new and new != order.price_paise:
                order.repriced = True
                log.info("entry %s not filled in time: re-pricing %s -> %s", order.cid, order.price_paise, new)
                self.modify(order, price_paise=new)
                self._ttl[order.local_id] = self.clock.call_later(self.cfg.execution.entry_ttl_ms / 1000.0,
                                                                  self._ttl_check, order)
                return
        log.info("entry %s not filled in time: cancelling", order.cid)
        self.cancel(order)

    # ---- cancel / modify -----------------------------------------------------------------------------
    def cancel(self, order: Order, leg: str | None = None) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if order.is_done and leg is None:
            fut.set_result(False)
            return fut
        if not order.broker_id:
            if order.status is OrderStatus.QUEUED:  # never left: just drop it
                self._transition(order, OrderStatus.CANCELLED, "cancelled before sending")
                fut.set_result(True)
            else:
                order.meta["cancel_requested"] = True
                fut.set_result(True)
            return fut

        async def action() -> None:
            res = await self.broker.cancel(order, leg)
            if not res.ok:
                log.warning("cancel %s%s failed: %s", order.cid, f" {leg}" if leg else "", res.error)
            if not fut.done():
                fut.set_result(res.ok)

        self._gate(action, priority=True)
        return fut

    def modify(self, order: Order, *, price_paise: int | None = None, qty: int | None = None,
               trigger_paise: int | None = None, leg: str | None = None) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if order.is_done or not order.broker_id or order.modify_count >= MAX_MODIFICATIONS:
            fut.set_result(False)
            return fut
        order.modify_count += 1

        async def action() -> None:
            res = await self.broker.modify(order, price_paise=price_paise, qty=qty, trigger_paise=trigger_paise,
                                           leg=leg)
            if res.ok:
                if leg in (None, LEG_ENTRY):
                    if price_paise is not None:
                        order.price_paise = price_paise
                    if trigger_paise is not None:
                        order.trigger_paise = trigger_paise
                    if qty:
                        order.qty = qty
                elif leg == LEG_TARGET and price_paise is not None:
                    order.target_paise = price_paise
                elif leg == LEG_STOP and trigger_paise is not None:
                    order.sl_paise = trigger_paise
                self._emit(OrderEvent("modified", order))
            else:
                log.warning("modify %s failed: %s", order.cid, res.error)
            if not fut.done():
                fut.set_result(res.ok)

        if not self._gate(action, priority=order.intent is Intent.EXIT or order.role in ("squareoff", "flatten")):
            order.modify_count -= 1
            fut.set_result(False)
        return fut

    # ---- waiting / queries -----------------------------------------------------------------------------
    def _check_waiters(self) -> None:
        if not self._waiters:
            return
        keep = []
        for cond, fut in self._waiters:
            if fut.done():
                continue
            try:
                ok = cond()
            except Exception:
                ok = True
            if ok:
                fut.set_result(True)
            else:
                keep.append((cond, fut))
        self._waiters = keep

    async def wait_until(self, cond: Callable[[], bool], timeout: float) -> bool:
        if cond():
            return True
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append((cond, fut))
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout)
            return True
        except asyncio.TimeoutError:
            return cond()

    def working(self, inst: Instrument | None = None) -> list[Order]:
        return [o for o in self.orders.values()
                if not o.is_done and o.status is not OrderStatus.NEW and (inst is None or o.instrument.key == inst.key)]

    def super_exit_legs_working(self, order: Order) -> bool:
        if order.kind is not OrderKind.SUPER or order.filled_qty == 0:
            return False
        legs = order.meta.get("leg_status", {})
        if not legs:
            return order.exit_filled_qty < order.filled_qty
        return any(not s.is_terminal for s in legs.values())

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.orders.values():
            out[o.status.value] = out.get(o.status.value, 0) + 1
        return out

    async def close(self) -> None:
        if self._drain_handle is not None:
            self._drain_handle.cancel()  # type: ignore[attr-defined]
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
