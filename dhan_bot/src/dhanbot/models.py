"""Core domain objects shared by every layer.

Prices are carried internally as integer *paise* (1 rupee = 100 paise) wherever exact
arithmetic matters (limit/trigger prices, tick rounding). Average fill prices are floats in
paise because averages are rarely whole numbers.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, enum.Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LOSS = "STOP_LOSS"  # SL-limit
    STOP_LOSS_MARKET = "STOP_LOSS_MARKET"  # SL-M


class Product(str, enum.Enum):
    CNC = "CNC"
    INTRADAY = "INTRADAY"
    MARGIN = "MARGIN"
    MTF = "MTF"


class Validity(str, enum.Enum):
    DAY = "DAY"
    IOC = "IOC"


class Intent(str, enum.Enum):
    ENTRY = "ENTRY"  # opens or increases a position
    EXIT = "EXIT"  # reduces a position (reduce-only)


class OrderKind(str, enum.Enum):
    REGULAR = "REGULAR"
    SUPER = "SUPER"  # Dhan super order: entry + target + stop-loss (+ trailing) legs held at the broker


class OrderStatus(str, enum.Enum):
    NEW = "NEW"  # created locally, not yet checked
    QUEUED = "QUEUED"  # passed risk, waiting for a rate-limit token
    SENT = "SENT"  # request written to the wire, no response yet
    UNKNOWN = "UNKNOWN"  # request timed out / connection dropped; being resolved by correlationId
    OPEN = "OPEN"  # acknowledged by broker (TRANSIT / PENDING / TRIGGERED)
    PART_FILLED = "PART_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"  # rejected locally (risk) or by broker/exchange - see Order.reason
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"  # we could not place it (e.g. confirmed not found after a timeout)

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def rank(self) -> int:
        return _RANK[self]


_TERMINAL = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.FAILED}
)
_RANK = {
    OrderStatus.NEW: 0,
    OrderStatus.QUEUED: 1,
    OrderStatus.SENT: 2,
    OrderStatus.UNKNOWN: 2,
    OrderStatus.OPEN: 3,
    OrderStatus.PART_FILLED: 4,
    OrderStatus.FILLED: 5,
    OrderStatus.CANCELLED: 5,
    OrderStatus.REJECTED: 5,
    OrderStatus.EXPIRED: 5,
    OrderStatus.FAILED: 5,
}

LEG_ENTRY = "ENTRY_LEG"
LEG_TARGET = "TARGET_LEG"
LEG_STOP = "STOP_LOSS_LEG"


@dataclass(frozen=True, slots=True)
class Instrument:
    security_id: int
    segment: str  # API exchangeSegment string, e.g. "NSE_EQ"
    feed_code: int  # numeric segment used by the binary market feed
    trading_symbol: str
    name: str = ""
    instrument_type: str = ""  # EQUITY, INDEX, FUTIDX, OPTIDX, FUTSTK, OPTSTK, FUTCOM, ...
    lot_size: int = 1
    tick_paise: int = 5
    expiry: date | None = None
    strike_paise: int = 0
    option_type: str = ""  # "CE" / "PE" / ""
    underlying: str = ""
    freeze_qty: int = 0  # 0 = unknown / not applicable
    multiplier: int = 1  # contract multiplier (MCX); 1 elsewhere

    @property
    def key(self) -> tuple[int, int]:
        """Hot-path lookup key: (feed segment code, security id)."""
        return (self.feed_code, self.security_id)

    @property
    def tradable(self) -> bool:
        return self.segment != "IDX_I"

    @property
    def is_option(self) -> bool:
        return self.option_type in ("CE", "PE")

    @property
    def is_derivative(self) -> bool:
        return self.segment.endswith("FNO") or self.segment in ("MCX_COMM", "NSE_CURRENCY", "BSE_CURRENCY")

    def round_to_tick(self, paise: float, direction: str = "nearest") -> int:
        """Round a price in paise to this instrument's tick. direction: nearest | up | down."""
        t = self.tick_paise or 1
        q = paise / t
        if direction == "up":
            n = math.ceil(q - 1e-9)
        elif direction == "down":
            n = math.floor(q + 1e-9)
        else:
            n = math.floor(q + 0.5)
        return int(n * t)


def rupees_to_paise(rupees: float) -> int:
    return int(round(rupees * 100))


def paise_to_rupees(paise: float) -> float:
    return round(paise / 100.0, 2)


@dataclass(slots=True)
class Order:
    local_id: int
    instrument: Instrument
    side: Side
    qty: int
    order_type: OrderType = OrderType.LIMIT
    product: Product = Product.INTRADAY
    validity: Validity = Validity.DAY
    intent: Intent = Intent.ENTRY
    kind: OrderKind = OrderKind.REGULAR
    price_paise: int = 0
    trigger_paise: int = 0
    target_paise: int = 0  # super-order target (absolute price)
    sl_paise: int = 0  # super-order / bracket stop-loss (absolute price)
    trail_paise: int = 0  # super-order trailing jump
    strategy: str = ""
    tag: str = ""
    role: str = "entry"  # entry | exit | sl | target | squareoff | flatten
    parent_id: int | None = None
    cid: str = ""
    broker_id: str = ""
    status: OrderStatus = OrderStatus.NEW
    filled_qty: int = 0  # entry-leg fills (the order's own direction)
    avg_fill_paise: float = 0.0
    exit_filled_qty: int = 0  # super-order exit-leg fills (opposite direction)
    reason: str = ""
    created_ns: int = 0
    sent_ns: int = 0
    ack_ns: int = 0
    done_ns: int = 0
    modify_count: int = 0
    repriced: bool = False
    resent: bool = False
    legs: dict[str, tuple[int, float]] = field(default_factory=dict)  # leg -> (cum filled, avg paise)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.qty - self.filled_qty)

    @property
    def is_done(self) -> bool:
        return self.status.is_terminal

    @property
    def is_working(self) -> bool:
        return not self.status.is_terminal and self.status is not OrderStatus.NEW

    @property
    def price(self) -> float:
        return paise_to_rupees(self.price_paise)

    @property
    def avg_price(self) -> float:
        return paise_to_rupees(self.avg_fill_paise)

    @property
    def symbol(self) -> str:
        return self.instrument.trading_symbol

    def __repr__(self) -> str:  # compact, log-friendly
        return (
            f"Order#{self.local_id}({self.side.value} {self.qty} {self.instrument.trading_symbol} "
            f"{self.order_type.value}@{self.price} {self.status.value} filled={self.filled_qty} "
            f"cid={self.cid} bid={self.broker_id})"
        )


@dataclass(slots=True)
class Fill:
    order_local_id: int
    instrument: Instrument
    product: Product
    side: Side
    qty: int
    price_paise: float
    leg: str
    ts: float


@dataclass(slots=True)
class OrderEvent:
    """What strategies receive in on_order(). kind: ack|fill|cancelled|rejected|expired|failed|unknown|modified."""

    kind: str
    order: Order
    fill: Fill | None = None


@dataclass(slots=True)
class BrokerUpdate:
    """A normalised snapshot of one order as reported by the broker (REST or order-update WebSocket)."""

    broker_id: str
    cid: str = ""
    status: OrderStatus | None = None
    raw_status: str = ""
    filled_qty: int | None = None
    avg_price: float | None = None  # rupees
    last_price: float | None = None  # rupees, last traded price of this order
    remaining: int | None = None
    qty: int | None = None
    price: float | None = None
    trigger: float | None = None
    reason: str = ""
    leg: str | None = None
    side: Side | None = None
    security_id: int | None = None
    segment: str | None = None
    product: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class Bar:
    key: tuple[int, int]
    symbol: str
    tf: int  # seconds
    start: float  # epoch seconds (UTC) of bar open
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def end(self) -> float:
        return self.start + self.tf
