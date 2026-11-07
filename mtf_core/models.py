"""Plain data types used across mtf_core. No calculation logic lives here."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional

DAYS_PER_YEAR = 365


@dataclass(frozen=True)
class Stock:
    """One watchlist entry.

    margin_pct is the % of the trade value YOU pay upfront for MTF on this
    stock (the broker funds the rest). It varies by stock and by broker.
    """

    symbol: str
    price: float
    margin_pct: float

    def with_price(self, price: float) -> "Stock":
        return Stock(self.symbol, float(price), self.margin_pct)


@dataclass(frozen=True)
class Slab:
    """Interest slab: `rate` % p.a. applies when the total borrowed <= `upto`.

    upto=None means the slab has no upper limit.
    """

    upto: Optional[float]
    rate: float

    def covers(self, amount: float) -> bool:
        return self.upto is None or amount <= self.upto


@dataclass(frozen=True)
class Position:
    """The largest MTF position for one stock under a borrow limit."""

    symbol: str
    price: float
    margin_pct: float
    qty: int
    trade_value: float      # qty * price
    own_margin: float       # what you pay
    borrowed: float         # what the broker funds
    rate: float             # % p.a. applied to `borrowed`
    interest_per_day: float

    def interest_for(self, days: int) -> float:
        return self.interest_per_day * days

    @property
    def interest_30d(self) -> float:
        return self.interest_for(30)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["interest_30d"] = self.interest_30d
        return d


@dataclass(frozen=True)
class Totals:
    """Sums over every position in a Plan (only filled for split plans)."""

    qty: int
    trade_value: float
    own_margin: float
    borrowed: float
    rate: float
    interest_per_day: float

    @property
    def interest_30d(self) -> float:
        return self.interest_per_day * 30

    def to_dict(self) -> dict:
        d = asdict(self)
        d["interest_30d"] = self.interest_30d
        return d


@dataclass(frozen=True)
class Plan:
    """Result of mtf_core.plan(): one Position per stock, plus totals."""

    platform: str
    max_borrow: float
    split: bool
    positions: List[Position] = field(default_factory=list)
    totals: Optional[Totals] = None

    @property
    def borrowed(self) -> float:
        """Amount the interest comparison should be based on."""
        return self.totals.borrowed if self.totals else self.max_borrow

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "max_borrow": self.max_borrow,
            "split": self.split,
            "positions": [p.to_dict() for p in self.positions],
            "totals": self.totals.to_dict() if self.totals else None,
        }


@dataclass(frozen=True)
class PlatformCost:
    """Interest cost of borrowing an amount on one platform."""

    platform: str
    rate: float
    interest_per_day: float

    @property
    def interest_30d(self) -> float:
        return self.interest_per_day * 30

    @property
    def interest_year(self) -> float:
        return self.interest_per_day * DAYS_PER_YEAR

    def to_dict(self) -> dict:
        d = asdict(self)
        d["interest_30d"] = self.interest_30d
        d["interest_year"] = self.interest_year
        return d
