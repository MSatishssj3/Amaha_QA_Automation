"""Getting watchlist data in and out: CSV files and live prices.

Live prices come from Yahoo Finance via the optional `yfinance` package. Any
bot can plug in its own source instead (a broker API, a cache, a test stub):
a price fetcher is just a function  symbols -> {symbol: price}.
"""

from __future__ import annotations

import csv
import logging
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .models import Stock

log = logging.getLogger(__name__)

WATCHLIST_HEADER_COMMENTS = (
    "# price      -> current price per share in INR\n"
    "# margin_pct -> % of the trade value YOU pay upfront for MTF\n"
)


class PriceFetchError(RuntimeError):
    """Live prices could not be fetched at all (e.g. yfinance is not installed)."""


def load_watchlist(path: str) -> List[Stock]:
    """Read a watchlist CSV with columns symbol, price, margin_pct.

    Blank lines and lines starting with '#' are ignored. Symbols are upper-cased;
    missing numbers become 0.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    stocks = []
    for row in csv.DictReader(lines):
        symbol = (row.get("symbol") or "").strip().upper()
        if symbol:
            stocks.append(Stock(symbol, float(row.get("price") or 0),
                                float(row.get("margin_pct") or 0)))
    return stocks


def save_watchlist(stocks: Sequence[Stock], path: str, comments: bool = True) -> None:
    """Write stocks back to a watchlist CSV that load_watchlist can read."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        if comments:
            fh.write(WATCHLIST_HEADER_COMMENTS)
        w = csv.writer(fh)
        w.writerow(["symbol", "price", "margin_pct"])
        for s in stocks:
            w.writerow([s.symbol, s.price, s.margin_pct])


def fetch_live_prices(symbols: Sequence[str], exchange_suffix: str = ".NS") -> Dict[str, float]:
    """Latest close for each NSE symbol from Yahoo Finance.

    Returns only the symbols it could price; failures are logged. Raises
    PriceFetchError if yfinance is not installed. Use exchange_suffix=".BO" for BSE.
    """
    try:
        import yfinance as yf
    except ImportError as exc:
        raise PriceFetchError("live prices need yfinance: pip install yfinance") from exc

    prices: Dict[str, float] = {}
    for sym in symbols:
        try:
            hist = yf.Ticker(sym + exchange_suffix).history(period="5d")
            if not hist.empty:
                prices[sym] = float(hist["Close"].iloc[-1])
            else:
                log.warning("No price data for %s", sym)
        except Exception as exc:  # network or unknown symbol
            log.warning("Could not fetch %s: %s", sym, exc)
    return prices


PriceFetcher = Callable[[Sequence[str]], Mapping[str, float]]


def with_live_prices(stocks: Sequence[Stock], fetcher: Optional[PriceFetcher] = None) -> List[Stock]:
    """Copy of `stocks` with prices replaced by whatever `fetcher` returns.

    Stocks the fetcher could not price keep their existing price.
    """
    fetcher = fetcher or fetch_live_prices
    prices = fetcher([s.symbol for s in stocks])
    return [s.with_price(prices[s.symbol]) if prices.get(s.symbol) else s for s in stocks]
