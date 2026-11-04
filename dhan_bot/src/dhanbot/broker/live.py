"""Live broker: real orders through the Dhan REST API.

Hot path: a per-instrument payload template (constant fields pre-filled once) is copied, the
variable fields are set, serialised with orjson and written on an already-open keep-alive
connection. Fills / acks arrive asynchronously via the order-update WebSocket, which the engine
connects to ``OrderManager.on_broker_update``.
"""

from __future__ import annotations

import logging

import orjson

from ..config import ExecutionCfg
from ..dhan.normalize import normalise_order, status_from_text
from ..dhan.rest import DhanRest
from ..errors import Category
from ..models import LEG_ENTRY, LEG_STOP, LEG_TARGET, BrokerUpdate, Order, OrderKind, OrderType
from .base import Broker, BrokerResult, LookupFailed, PlaceResult

log = logging.getLogger(__name__)


def _rupees(paise: int) -> float:
    return round(paise / 100.0, 2)


class LiveBroker(Broker):
    name = "live"

    def __init__(self, rest: DhanRest, execution: ExecutionCfg) -> None:
        super().__init__()
        self.rest = rest
        self.exec = execution
        self._tpl: dict[tuple, dict] = {}
        self._super_tpl: dict[tuple, dict] = {}

    def _template(self, order: Order) -> dict:
        inst = order.instrument
        k = (inst.feed_code, inst.security_id, order.product.value, order.validity.value)
        t = self._tpl.get(k)
        if t is None:
            t = self._tpl[k] = {
                "dhanClientId": self.rest.client_id, "exchangeSegment": inst.segment,
                "securityId": str(inst.security_id), "productType": order.product.value,
                "validity": order.validity.value, "disclosedQuantity": 0, "afterMarketOrder": False,
            }
        return t

    def _super_template(self, order: Order) -> dict:
        inst = order.instrument
        k = (inst.feed_code, inst.security_id, order.product.value)
        t = self._super_tpl.get(k)
        if t is None:
            t = self._super_tpl[k] = {
                "dhanClientId": self.rest.client_id, "exchangeSegment": inst.segment,
                "securityId": str(inst.security_id), "productType": order.product.value,
            }
        return t

    def build_body(self, order: Order) -> bytes:
        if order.kind is OrderKind.SUPER:
            body = dict(self._super_template(order))
            body["transactionType"] = order.side.value
            body["orderType"] = order.order_type.value
            body["quantity"] = order.qty
            body["price"] = _rupees(order.price_paise)
            body["targetPrice"] = _rupees(order.target_paise)
            body["stopLossPrice"] = _rupees(order.sl_paise)
            body["trailingJump"] = _rupees(order.trail_paise)
            body["correlationId"] = order.cid
            return orjson.dumps(body)
        body = dict(self._template(order))
        body["transactionType"] = order.side.value
        body["orderType"] = order.order_type.value
        body["quantity"] = order.qty
        body["price"] = 0.0 if order.order_type in (OrderType.MARKET, OrderType.STOP_LOSS_MARKET) else _rupees(order.price_paise)
        body["triggerPrice"] = _rupees(order.trigger_paise)
        body["correlationId"] = order.cid
        return orjson.dumps(body)

    def prewarm(self, order: Order) -> None:
        """Build templates ahead of time (called at preflight for every tradable instrument)."""
        self._template(order)
        self._super_template(order)

    async def place(self, order: Order) -> PlaceResult:
        body = self.build_body(order)
        if order.kind is OrderKind.SUPER:
            res = await self.rest.place_super_raw(body)
        else:
            slicing = self.exec.allow_slicing and order.instrument.freeze_qty and order.qty > order.instrument.freeze_qty
            res = await self.rest.place_order_raw(body, slicing=bool(slicing))
        if not res.ok:
            return PlaceResult(False, error=res.error, rtt_ns=res.rtt_ns)
        data = res.data
        if isinstance(data, list):  # slicing returns one entry per slice
            data = data[0] if data else {}
        data = data if isinstance(data, dict) else {}
        oid = str(data.get("orderId") or "")
        return PlaceResult(True, oid, status_from_text(str(data.get("orderStatus") or "")), rtt_ns=res.rtt_ns)

    async def modify(self, order: Order, *, price_paise: int | None = None, qty: int | None = None,
                     trigger_paise: int | None = None, leg: str | None = None) -> BrokerResult:
        if order.kind is OrderKind.SUPER:
            leg = leg or LEG_ENTRY
            payload: dict = {"legName": leg}
            if leg == LEG_ENTRY:
                payload.update(orderType=order.order_type.value, quantity=qty or order.qty,
                               price=_rupees(price_paise if price_paise is not None else order.price_paise),
                               targetPrice=_rupees(order.target_paise), stopLossPrice=_rupees(order.sl_paise),
                               trailingJump=_rupees(order.trail_paise))
            elif leg == LEG_TARGET:
                payload["targetPrice"] = _rupees(price_paise if price_paise is not None else order.target_paise)
            elif leg == LEG_STOP:
                payload.update(stopLossPrice=_rupees(trigger_paise if trigger_paise is not None else order.sl_paise),
                               trailingJump=_rupees(order.trail_paise))
            res = await self.rest.modify_super(order.broker_id, payload)
        else:
            payload = {"orderType": order.order_type.value, "legName": "",
                       "quantity": qty or order.qty, "disclosedQuantity": 0, "validity": order.validity.value,
                       "price": _rupees(price_paise if price_paise is not None else order.price_paise),
                       "triggerPrice": _rupees(trigger_paise if trigger_paise is not None else order.trigger_paise)}
            res = await self.rest.modify_order(order.broker_id, payload)
        return BrokerResult(res.ok, res.error, res.rtt_ns)

    async def cancel(self, order: Order, leg: str | None = None) -> BrokerResult:
        if order.kind is OrderKind.SUPER:
            res = await self.rest.cancel_super(order.broker_id, leg or LEG_ENTRY)
        else:
            res = await self.rest.cancel_order(order.broker_id)
        return BrokerResult(res.ok, res.error, res.rtt_ns)

    async def lookup(self, cid: str) -> BrokerUpdate | None:
        res = await self.rest.get_order_by_cid(cid)
        if res.ok:
            data = res.data
            if isinstance(data, list):
                data = data[0] if data else None
            if not isinstance(data, dict):
                return None
            return normalise_order(data)
        if res.error and res.error.category in (Category.NETWORK, Category.SERVER, Category.RATE_LIMIT, Category.AUTH):
            raise LookupFailed(str(res.error))
        return None  # 4xx "not found"

    async def positions(self) -> list[dict] | None:
        res = await self.rest.get_positions()
        if not res.ok:
            log.warning("GET /positions failed: %s", res.error)
            return None
        return res.data if isinstance(res.data, list) else []

    async def orders(self) -> list[BrokerUpdate] | None:
        res = await self.rest.get_orders()
        if not res.ok:
            return None
        out = []
        for row in res.data if isinstance(res.data, list) else []:
            u = normalise_order(row) if isinstance(row, dict) else None
            if u is not None:
                out.append(u)
        return out

    async def exit_all(self) -> bool:
        res = await self.rest.exit_all_positions()
        if not res.ok:
            log.error("DELETE /positions (exit all) failed: %s", res.error)
        return res.ok
