"""Normalise broker order/position payloads (REST and order-update WebSocket) into one shape.

The REST order book uses camelCase (``orderId``, ``orderStatus``, ``filledQty`` ...) while the
order-update socket uses PascalCase (``OrderNo``, ``Status``, ``TradedQty`` ...). Field names on the
socket could not be verified against the live service from the build sandbox, so lookups are
case-insensitive and accept every known alias; unknown status strings map to ``None`` (= "no status
change") and are logged once by the caller.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import LEG_ENTRY, LEG_STOP, LEG_TARGET, BrokerUpdate, OrderStatus, Side

_STATUS = {
    "transit": OrderStatus.OPEN,
    "pending": OrderStatus.OPEN,
    "open": OrderStatus.OPEN,
    "triggered": OrderStatus.OPEN,
    "modified": OrderStatus.OPEN,
    "traded": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "complete": OrderStatus.FILLED,
    "completed": OrderStatus.FILLED,
    "parttraded": OrderStatus.PART_FILLED,
    "partiallytraded": OrderStatus.PART_FILLED,
    "partiallyfilled": OrderStatus.PART_FILLED,
    "partfilled": OrderStatus.PART_FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}
_NON_ALPHA = re.compile(r"[^a-z]")
_LEG_NAMES = {"ENTRY_LEG": LEG_ENTRY, "TARGET_LEG": LEG_TARGET, "STOP_LOSS_LEG": LEG_STOP}
_LEG_NUMBERS = {1: LEG_ENTRY, 2: LEG_STOP, 3: LEG_TARGET}

_ALIASES = {
    "broker_id": ("orderid", "orderno"),
    "cid": ("correlationid",),
    "status": ("orderstatus", "status"),
    "filled": ("filledqty", "tradedqty", "tradedquantity"),
    "avg": ("averagetradedprice", "avgtradedprice", "avgprice"),
    "last": ("tradedprice", "lasttradedprice"),
    "remaining": ("remainingquantity", "remainingqty", "pendingqty"),
    "qty": ("quantity", "qty"),
    "price": ("price",),
    "trigger": ("triggerprice",),
    "reason": ("omserrordescription", "reasondescription", "remarks", "rejectionreason"),
    "legname": ("legname",),
    "legno": ("legno",),
    "side": ("transactiontype", "txntype"),
    "security_id": ("securityid",),
    "segment": ("exchangesegment",),
    "product": ("producttype", "product"),
}


def status_from_text(raw: str) -> OrderStatus | None:
    return _STATUS.get(_NON_ALPHA.sub("", (raw or "").lower()))


def _pick(low: dict[str, Any], field: str) -> Any:
    for alias in _ALIASES[field]:
        v = low.get(alias)
        if v not in (None, ""):
            return v
    return None


def _int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalise_order(data: dict[str, Any]) -> BrokerUpdate | None:
    low = {str(k).lower(): v for k, v in data.items()}
    broker_id = _pick(low, "broker_id")
    if broker_id in (None, ""):
        return None
    raw_status = str(_pick(low, "status") or "")
    leg: str | None = None
    ln = _pick(low, "legname")
    if ln and str(ln).upper() in _LEG_NAMES:
        leg = _LEG_NAMES[str(ln).upper()]
    else:
        lno = _int(_pick(low, "legno"))
        if lno in _LEG_NUMBERS:
            leg = _LEG_NUMBERS[lno]  # type: ignore[index]
    side_raw = str(_pick(low, "side") or "").upper()
    side = Side.BUY if side_raw in ("BUY", "B") else Side.SELL if side_raw in ("SELL", "S") else None
    return BrokerUpdate(
        broker_id=str(broker_id),
        cid=str(_pick(low, "cid") or ""),
        status=status_from_text(raw_status),
        raw_status=raw_status,
        filled_qty=_int(_pick(low, "filled")),
        avg_price=_float(_pick(low, "avg")),
        last_price=_float(_pick(low, "last")),
        remaining=_int(_pick(low, "remaining")),
        qty=_int(_pick(low, "qty")),
        price=_float(_pick(low, "price")),
        trigger=_float(_pick(low, "trigger")),
        reason=str(_pick(low, "reason") or ""),
        leg=leg,
        side=side,
        security_id=_int(_pick(low, "security_id")),
        segment=(str(_pick(low, "segment")) if _pick(low, "segment") else None),
        product=(str(_pick(low, "product")) if _pick(low, "product") else None),
        raw=data,
    )


def available_balance(funds: dict[str, Any]) -> float | None:
    """Dhan's fund-limit response famously spells it ``availabelBalance``; accept both."""
    for k in ("availabelBalance", "availableBalance", "available_balance"):
        if k in funds:
            return _float(funds[k])
    return None
