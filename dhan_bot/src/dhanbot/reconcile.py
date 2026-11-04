"""Broker reconciliation (live mode).

* Startup: adopt this bot's orders from today's order book (correlationId prefix) and take the
  broker's positions for the instruments the bot trades - this is crash recovery.
* Periodic: re-read the order book (missed WebSocket updates are applied; fills are idempotent),
  compare positions and refresh funds. While the order-update socket is down the order book is
  polled every ``fast_poll_s``.

Position drift is only *adopted* when it is seen in two consecutive snapshots and the instrument
had no fills in between - a snapshot racing with a fill must not double-count.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .broker.live import LiveBroker
from .config import AppConfig
from .dhan.normalize import available_balance
from .dhan.rest import DhanRest
from .models import Instrument, OrderEvent
from .oms import OrderManager
from .positions import PositionBook, broker_pnl_inr
from .risk import RiskEngine

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, cfg: AppConfig, rest: DhanRest, broker: LiveBroker, oms: OrderManager,
                 positions: PositionBook, risk: RiskEngine,
                 resolve: Callable[[str | None, int | None], Instrument | None],
                 bot_keys: Callable[[], set[tuple[int, int, str]]],
                 order_ws_connected: Callable[[], bool], alert: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.rc = cfg.reconcile
        self.rest = rest
        self.broker = broker
        self.oms = oms
        self.positions = positions
        self.risk = risk
        self.resolve = resolve
        self.bot_keys = bot_keys
        self.order_ws_connected = order_ws_connected
        self.alert = alert or (lambda msg: None)
        self._last_fill: dict[tuple[int, int], float] = {}
        self._pending_drift: dict[tuple, int] = {}
        self._stop = False
        oms.listeners.append(self._on_event)

    def _on_event(self, ev: OrderEvent) -> None:
        if ev.kind == "fill":
            self._last_fill[ev.order.instrument.key] = time.monotonic()

    def _resolve_seg(self, seg: str, sid: int) -> Instrument | None:
        return self.resolve(seg, sid)

    async def startup(self) -> dict:
        report = {"adopted_orders": 0, "positions": []}
        ups = await self.broker.orders()
        if ups is None:
            raise RuntimeError("could not read today's order book from Dhan")
        for u in ups:
            if u.cid.startswith(self.cfg.dhan.cid_prefix):
                known = self.oms.find(u) is not None
                self.oms.ingest_snapshot(u)
                report["adopted_orders"] += 0 if known else 1
        rows = await self.broker.positions()
        if rows is None:
            raise RuntimeError("could not read positions from Dhan")
        keys = self.bot_keys()
        drift = self.positions.reconcile(rows, self._resolve_seg, adopt=True, only_keys=keys)
        report["positions"] = [p.to_dict() for p in self.positions.open_positions()]
        if drift:
            log.warning("startup: adopted broker positions: %s", drift)
        manual = [r for r in rows if int(r.get("netQty") or 0) != 0 and
                  (lambda i: i is None or (i.feed_code, i.security_id, str(r.get("productType"))) not in keys)(
                      self.resolve(str(r.get("exchangeSegment")), int(r.get("securityId") or 0)))]
        if manual:
            log.warning("startup: %d open position(s) not managed by the bot (left untouched): %s", len(manual),
                        [r.get("tradingSymbol") for r in manual])
        report["manual_positions"] = len(manual)
        await self.refresh_funds()
        return report

    async def refresh_funds(self) -> None:
        res = await self.rest.get_funds()
        if res.ok and isinstance(res.data, dict):
            bal = available_balance(res.data)
            if bal is not None:
                self.risk.funds_available_inr = bal

    async def _orders_once(self) -> None:
        ups = await self.broker.orders()
        if ups is None:
            return
        for u in ups:
            if u.cid.startswith(self.cfg.dhan.cid_prefix) or self.oms.find(u) is not None:
                self.oms.on_broker_update(u)

    async def _positions_once(self) -> None:
        rows = await self.broker.positions()
        if rows is None:
            return
        if self.cfg.risk.include_manual_trades:
            self.risk.broker_pnl_inr = broker_pnl_inr(rows)
        keys = self.bot_keys()
        diffs = self.positions.diff(rows, self._resolve_seg, only_keys=keys)
        now = time.monotonic()
        seen = set()
        for d in diffs:
            sig = (d.key, d.local, d.broker)
            seen.add(sig)
            quiet = now - self._last_fill.get((d.key[0], d.key[1]), 0) > 5.0
            if self._pending_drift.get(sig, 0) >= 1 and quiet:
                msg = f"position drift {d.inst.trading_symbol}: bot={d.local} broker={d.broker}"
                if self.rc.adopt_broker_qty:
                    self.positions.adopt(d)
                    msg += " -> adopted broker quantity"
                log.error(msg)
                self.alert(msg)
                self._pending_drift.pop(sig, None)
            else:
                self._pending_drift[sig] = self._pending_drift.get(sig, 0) + 1
        for sig in list(self._pending_drift):
            if sig not in seen:
                self._pending_drift.pop(sig, None)
        self.risk.check_pnl()

    async def run(self) -> None:
        last_orders = last_pos = last_funds = 0.0
        while not self._stop:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            interval = self.rc.orders_interval_s if self.order_ws_connected() else self.rc.fast_poll_s
            try:
                if now - last_orders >= interval:
                    last_orders = now
                    await self._orders_once()
                if now - last_pos >= self.rc.positions_interval_s:
                    last_pos = now
                    await self._positions_once()
                if now - last_funds >= self.rc.funds_interval_s:
                    last_funds = now
                    await self.refresh_funds()
            except Exception:
                log.exception("reconcile cycle failed")

    def stop(self) -> None:
        self._stop = True
