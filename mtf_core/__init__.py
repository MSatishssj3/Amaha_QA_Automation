"""mtf_core: reusable MTF (Margin Trading Facility) calculations for bots.

Quick start:

    from mtf_core import load_watchlist, plan, compare_platforms

    stocks = load_watchlist("watchlist.csv")
    result = plan(stocks, max_borrow=1_00_000, platform="Pocketful")
    for p in result.positions:
        print(p.symbol, p.qty, p.trade_value, p.interest_30d)
    for c in compare_platforms(result.borrowed):
        print(c.platform, c.rate, c.interest_30d)

Main pieces:
    plan(), max_position(), compare_platforms(), MTFCalculator   calculator.py
    interest_rate(), next_slab(), register_platform(), load_rates() rates.py
    load_watchlist(), save_watchlist(), fetch_live_prices(),
    with_live_prices()                                            data.py
    inr(), rupees(), amount_in_words(), short_amount()           formatting.py
    Stock, Position, Plan, Totals, PlatformCost, Slab            models.py
"""

from . import rates
from .calculator import (
    DEFAULT_MAX_BORROW,
    MAX_BORROW,
    MIN_BORROW,
    MTFCalculator,
    compare_platforms,
    max_position,
    plan,
    validate_borrow,
)
from .data import (
    PriceFetchError,
    fetch_live_prices,
    load_watchlist,
    save_watchlist,
    with_live_prices,
)
from .formatting import amount_in_words, inr, rupees, short_amount
from .models import Plan, PlatformCost, Position, Slab, Stock, Totals
from .rates import (
    PLATFORMS,
    daily_interest,
    interest_rate,
    load_rates,
    next_slab,
    register_platform,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_MAX_BORROW", "MAX_BORROW", "MIN_BORROW", "PLATFORMS",
    "MTFCalculator", "Plan", "PlatformCost", "Position", "PriceFetchError",
    "Slab", "Stock", "Totals",
    "amount_in_words", "compare_platforms", "daily_interest", "fetch_live_prices",
    "inr", "interest_rate", "load_rates", "load_watchlist", "max_position",
    "next_slab", "plan", "rates", "register_platform", "rupees", "save_watchlist",
    "short_amount", "validate_borrow", "with_live_prices",
]
