"""
MTF Calculator Bot (command line)
=================================

For every stock in your watchlist, prints the maximum number of shares you can
buy using MTF so the amount BORROWED stays under a limit (Rs 100 to Rs 2 crore,
Rs 1 lakh by default), and compares the interest on Pocketful, Dhan, Groww and
Zerodha. All calculation and data loading lives in the reusable `mtf_core`
package; this file only handles the command line and the text table.

For the interactive slider page, open mtf_calculator.html in a browser.

Usage examples:
    python mtf_calculator.py
    python mtf_calculator.py --max-borrow 500000
    python mtf_calculator.py --capital 30000
    python mtf_calculator.py --split            # share the limit across all stocks
    python mtf_calculator.py --platform Dhan    # use Dhan's rates in the main table
    python mtf_calculator.py --live             # fetch live NSE prices (needs yfinance)
    python mtf_calculator.py --csv results.csv  # also save the table to CSV
"""

import argparse
import csv
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import mtf_core
except ImportError:  # running from a checkout without `pip install -e .`
    sys.path.insert(0, os.path.dirname(HERE))
    import mtf_core

from mtf_core import inr

DEFAULT_WATCHLIST = os.path.join(HERE, "watchlist.csv")

COLUMNS = [
    ("Stock", "symbol", str),
    ("Price", "price", inr),
    ("Margin %", "margin_pct", lambda v: f"{v:g}%"),
    ("Max Qty", "qty", str),
    ("Trade Value", "trade_value", inr),
    ("You Pay", "own_margin", inr),
    ("Borrowed", "borrowed", inr),
    ("Rate % p.a.", "rate", lambda v: f"{v:.2f}%"),
    ("Interest/Day", "interest_per_day", inr),
    ("Interest/30d", "interest_30d", inr),
]

COMPARE_COLUMNS = [
    ("Platform", "platform", str),
    ("Rate % p.a.", "rate", lambda v: f"{v:.2f}%"),
    ("Interest/Day", "interest_per_day", inr),
    ("Interest/30d", "interest_30d", inr),
    ("Interest/Year", "interest_year", inr),
]


def render_table(items, columns, total_row=None):
    """Plain-text table of objects (read by attribute) with no dependencies."""
    body = [[fmt(getattr(it, key)) for _, key, fmt in columns] for it in items]
    if total_row:
        body.append(total_row)
    headers = [h for h, _, _ in columns]
    widths = [max(len(h), *(len(r[i]) for r in body)) if body else len(h)
              for i, h in enumerate(headers)]

    def line(cells):
        return "| " + " | ".join(c.ljust(widths[i]) if i == 0 else c.rjust(widths[i])
                                 for i, c in enumerate(cells)) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    out = [sep, line(headers), sep]
    for i, r in enumerate(body):
        if total_row and i == len(body) - 1:
            out.append(sep)
        out.append(line(r))
    out.append(sep)
    return "\n".join(out)


def save_csv(positions, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([h for h, _, _ in COLUMNS])
        for p in positions:
            w.writerow([getattr(p, key) if key == "symbol" else round(getattr(p, key), 2)
                        for _, key, _ in COLUMNS])


def amount_in_range(text):
    try:
        return mtf_core.validate_borrow(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be between {inr(mtf_core.MIN_BORROW)} and {inr(mtf_core.MAX_BORROW)} (2 crore)")


def main(argv=None):
    p = argparse.ArgumentParser(description="MTF max-quantity calculator for your watchlist")
    p.add_argument("--watchlist", default=DEFAULT_WATCHLIST, help="path to watchlist CSV")
    p.add_argument("--max-borrow", type=amount_in_range, default=mtf_core.DEFAULT_MAX_BORROW,
                   help="max amount to borrow in INR, 100 to 20000000 (2 crore); default 100000 (1 lakh)")
    p.add_argument("--rate", type=float, default=None,
                   help="override the interest rate, %% p.a. (default: the platform's slab rate)")
    p.add_argument("--platform", choices=list(mtf_core.PLATFORMS), default=mtf_core.rates.DEFAULT_PLATFORM,
                   help="whose interest rates to use in the main table (default Pocketful)")
    p.add_argument("--capital", type=float, default=None,
                   help="your own cash available for margin, in INR (optional cap)")
    p.add_argument("--split", action="store_true",
                   help="split --max-borrow (and --capital) equally across all stocks "
                        "so the TOTAL borrowed stays under the limit")
    p.add_argument("--live", action="store_true", help="fetch live NSE prices via yfinance")
    p.add_argument("--csv", metavar="FILE", help="also save the results table to a CSV file")
    args = p.parse_args(argv)

    stocks = mtf_core.load_watchlist(args.watchlist)
    if not stocks:
        sys.exit(f"No stocks found in {args.watchlist}")
    if args.live:
        logging.basicConfig(format="! %(message)s  (using watchlist price)")
        try:
            stocks = mtf_core.with_live_prices(stocks)
        except mtf_core.PriceFetchError as exc:
            print(f"! {exc}  (using CSV prices)\n")

    result = mtf_core.plan(stocks, args.max_borrow, platform=args.platform, split=args.split,
                           capital=args.capital, rate=args.rate)
    n = len(stocks) if args.split else 1

    print("MTF Calculator Bot")
    print("  Interest rate : " + (f"{args.rate}% p.a. (override)" if args.rate is not None
                                  else f"{args.platform} slab rate for the borrowed amount"))
    print(f"  Max borrow    : Rs {inr(args.max_borrow)}"
          + (f"  (split -> Rs {inr(args.max_borrow / n)} per stock)" if args.split
             else "  (per stock)"))
    if args.capital is not None:
        print(f"  Own capital   : Rs {inr(args.capital)}"
              + (f"  (split -> Rs {inr(args.capital / n)} per stock)" if args.split else ""))
    print()

    t = result.totals
    total_row = None
    if t:
        total_row = ["TOTAL", "", "", str(t.qty), inr(t.trade_value), inr(t.own_margin),
                     inr(t.borrowed), f"{t.rate:.2f}%", inr(t.interest_per_day), inr(t.interest_30d)]
    print(render_table(result.positions, COLUMNS, total_row))

    if not args.split:
        print("\nEach row is independent: it is the max you can buy of THAT stock alone.")
        print("Use --split to buy across all stocks with the total borrowed under the limit.")

    print(f"\nInterest on other platforms for borrowing Rs {inr(result.borrowed)} "
          f"(rates checked {mtf_core.rates.RATES_CHECKED}, cheapest first):")
    print(render_table(mtf_core.compare_platforms(result.borrowed), COMPARE_COLUMNS))
    print("Max qty is the same on every platform if the margin % is the same; "
          "only the interest differs.")

    if args.csv:
        save_csv(result.positions, args.csv)
        print(f"\nSaved to {args.csv}")


if __name__ == "__main__":
    main()
