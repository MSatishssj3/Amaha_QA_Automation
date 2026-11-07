"""Core MTF calculation: how many shares fit under a borrow limit, and what they cost.

For one stock (price P, margin m %):
    borrowed per share = P * (1 - m/100)        broker funds this
    own per share      = P * m/100              you pay this
    max qty            = floor(limit / borrowed per share)
                         (and <= capital / own per share, if capital is given)
    interest per day   = borrowed * rate / 100 / 365
"""

from __future__ import annotations

import math
from typing import Callable, Iterable, List, Mapping, Optional, Sequence

from . import rates
from .models import Plan, PlatformCost, Position, Stock, Totals

DEFAULT_MAX_BORROW = 1_00_000.0          # Rs 1 lakh
MIN_BORROW = 100.0                       # Rs 100
MAX_BORROW = 2_00_00_000.0               # Rs 2 crore

_EPS = 1e-9  # absorbs float error so e.g. 100000 / 750 gives 133, not 132


def validate_borrow(amount: float, low: float = MIN_BORROW, high: float = MAX_BORROW) -> float:
    """Return `amount` as float, or raise ValueError if it is outside [low, high]."""
    amount = float(amount)
    if not low <= amount <= high:
        raise ValueError(f"borrow amount must be between {low:,.0f} and {high:,.0f}")
    return amount


def max_position(
    stock: Stock,
    max_borrow: float,
    *,
    platform: Optional[str] = None,
    rate: Optional[float] = None,
    capital: Optional[float] = None,
) -> Position:
    """Largest MTF position in one stock whose borrowed amount stays <= max_borrow.

    rate=None uses the platform's slab rate for this position's borrowed amount.
    capital caps the position by the cash you have for your margin share.
    """
    price, margin = float(stock.price), float(stock.margin_pct)
    valid = price > 0 and 0 <= margin < 100
    own_per_share = price * margin / 100
    qty = 0
    if valid:
        qty = math.floor(max_borrow / (price * (1 - margin / 100)) + _EPS)
        if capital is not None and own_per_share > 0:
            qty = min(qty, math.floor(capital / own_per_share + _EPS))
        qty = max(qty, 0)

    trade_value = qty * price
    own = qty * own_per_share
    borrowed = trade_value - own
    if rate is None:
        rate = rates.interest_rate(borrowed, platform)
    return Position(
        symbol=stock.symbol,
        price=price,
        margin_pct=margin,
        qty=qty,
        trade_value=trade_value,
        own_margin=own,
        borrowed=borrowed,
        rate=rate,
        interest_per_day=rates.daily_interest(borrowed, rate),
    )


def plan(
    stocks: Sequence[Stock],
    max_borrow: float = DEFAULT_MAX_BORROW,
    *,
    platform: Optional[str] = None,
    split: bool = False,
    capital: Optional[float] = None,
    rate: Optional[float] = None,
) -> Plan:
    """Max MTF position for every stock in a watchlist.

    split=False: each position is the max for that stock on its own.
    split=True:  max_borrow (and capital) is shared equally across the stocks,
                 so the TOTAL borrowed stays under max_borrow; the slab rate is
                 then decided by the total borrowed.
    """
    platform = platform or rates.DEFAULT_PLATFORM
    n = len(stocks) if split and stocks else 1
    per_borrow = max_borrow / n
    per_capital = capital / n if capital is not None else None

    def build(r: Optional[float]) -> List[Position]:
        return [max_position(s, per_borrow, platform=platform, rate=r, capital=per_capital)
                for s in stocks]

    positions = build(rate)
    if not split:
        return Plan(platform, max_borrow, split, positions, None)

    book_rate = rate if rate is not None else rates.interest_rate(
        sum(p.borrowed for p in positions), platform)
    positions = build(book_rate)
    totals = Totals(
        qty=sum(p.qty for p in positions),
        trade_value=sum(p.trade_value for p in positions),
        own_margin=sum(p.own_margin for p in positions),
        borrowed=sum(p.borrowed for p in positions),
        rate=book_rate,
        interest_per_day=sum(p.interest_per_day for p in positions),
    )
    return Plan(platform, max_borrow, split, positions, totals)


def compare_platforms(borrowed: float, platforms: Optional[Iterable[str]] = None) -> List[PlatformCost]:
    """Interest cost of borrowing `borrowed` on each platform, cheapest first."""
    names = list(platforms) if platforms is not None else list(rates.PLATFORMS)
    costs = []
    for name in names:
        r = rates.interest_rate(borrowed, name)
        costs.append(PlatformCost(name, r, rates.daily_interest(borrowed, r)))
    return sorted(costs, key=lambda c: c.rate)


PriceFetcher = Callable[[Sequence[str]], Mapping[str, float]]


class MTFCalculator:
    """Convenience wrapper that remembers your settings between calls.

        calc = MTFCalculator(platform="Dhan", max_borrow=5_00_000)
        result = calc.plan(stocks)                 # uses the saved settings
        result = calc.plan(stocks, split=True)     # override one setting
        costs  = calc.compare(result)              # every platform, cheapest first
    """

    def __init__(
        self,
        platform: Optional[str] = None,
        max_borrow: float = DEFAULT_MAX_BORROW,
        *,
        split: bool = False,
        capital: Optional[float] = None,
        rate: Optional[float] = None,
        price_fetcher: Optional[PriceFetcher] = None,
    ):
        self.platform = platform or rates.DEFAULT_PLATFORM
        self.max_borrow = max_borrow
        self.split = split
        self.capital = capital
        self.rate = rate
        self.price_fetcher = price_fetcher

    def plan(self, stocks: Sequence[Stock], **overrides) -> Plan:
        settings = dict(max_borrow=self.max_borrow, platform=self.platform,
                        split=self.split, capital=self.capital, rate=self.rate)
        settings.update(overrides)
        if self.price_fetcher is not None:
            from .data import with_live_prices
            stocks = with_live_prices(stocks, fetcher=self.price_fetcher)
        return plan(stocks, **settings)

    def compare(self, plan_or_amount=None) -> List[PlatformCost]:
        if plan_or_amount is None:
            amount = self.max_borrow
        elif isinstance(plan_or_amount, Plan):
            amount = plan_or_amount.borrowed
        else:
            amount = float(plan_or_amount)
        return compare_platforms(amount)
