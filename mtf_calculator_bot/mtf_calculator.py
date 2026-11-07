"""
MTF Calculator Bot
==================

For every stock in your watchlist, works out the maximum number of shares you
can buy using MTF (Margin Trading Facility) such that the amount BORROWED from
the broker stays under a chosen limit (Rs 1 lakh by default, up to Rs 2 crore).
Pocketful charges 5.99% p.a. on MTF borrowings up to Rs 1 lakh, so the default
keeps you in the cheapest slab.

For a slider-based UI run:  streamlit run app.py

How the numbers are calculated (per stock):
    trade value   = qty * price
    your margin   = trade value * margin_pct / 100       (you pay this)
    borrowed      = trade value - your margin            (broker funds this)
    max qty       = largest qty with borrowed <= max borrow
                    (and your margin <= --capital, if given)
    interest/day  = borrowed * rate / 100 / 365
    rate          = slab rate for the borrowed amount (see INTEREST_SLABS),
                    unless --rate is given

Usage examples:
    python mtf_calculator.py
    python mtf_calculator.py --max-borrow 500000
    python mtf_calculator.py --capital 30000
    python mtf_calculator.py --split            # share the limit across all stocks
    python mtf_calculator.py --live             # fetch live NSE prices (needs yfinance)
    python mtf_calculator.py --csv results.csv  # also save the table to CSV

Only the Python standard library is required (yfinance is optional, for --live).
"""

import argparse
import csv
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WATCHLIST = os.path.join(HERE, "watchlist.csv")

# Borrow limit: default Rs 1 lakh, adjustable up to Rs 2 crore.
MIN_BORROW = 1_00_000.0
MAX_BORROW = 2_00_00_000.0
DEFAULT_MAX_BORROW = MIN_BORROW

# Pocketful MTF interest slabs as (borrowed amount up to, % p.a.).
# The slab is chosen by the total borrowed and that rate applies to all of it.
# Verify against the Pocketful app / MTF calculator and edit here if they change.
INTEREST_SLABS = [
    (1_00_000, 5.99),
    (25_00_000, 14.60),
    (float("inf"), 16.00),
]


def slab_rate(borrowed):
    """Interest rate (% p.a.) for a given borrowed amount."""
    for upto, rate in INTEREST_SLABS:
        if borrowed <= upto:
            return rate
    return INTEREST_SLABS[-1][1]


def load_watchlist(path):
    """Read the watchlist CSV, skipping blank lines and '#' comments."""
    with open(path, newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    stocks = []
    for row in csv.DictReader(lines):
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        stocks.append({
            "symbol": symbol,
            "price": float(row.get("price") or 0),
            "margin_pct": float(row.get("margin_pct") or 0),
        })
    return stocks


def fetch_live_prices(stocks, exchange_suffix=".NS"):
    """Overwrite prices with the latest close from Yahoo Finance (optional)."""
    try:
        import yfinance as yf
    except ImportError:
        print("! --live needs yfinance: pip install yfinance  (using CSV prices)\n")
        return
    for s in stocks:
        try:
            hist = yf.Ticker(s["symbol"] + exchange_suffix).history(period="5d")
            if not hist.empty:
                s["price"] = float(hist["Close"].iloc[-1])
        except Exception as exc:  # network / symbol errors -> keep CSV price
            print(f"! Could not fetch {s['symbol']}: {exc}  (using CSV price)")


def calculate(stock, max_borrow, rate=None, capital=None):
    """Return the max-qty MTF position for one stock.

    rate=None -> use the slab rate for this position's borrowed amount.
    """
    price = stock["price"]
    margin_pct = stock["margin_pct"]
    borrow_per_share = price * (1 - margin_pct / 100)
    own_per_share = price * margin_pct / 100

    if price <= 0 or not 0 <= margin_pct < 100:
        qty = 0
    else:
        qty = math.floor(max_borrow / borrow_per_share + 1e-9)
        if capital is not None and own_per_share > 0:
            qty = min(qty, math.floor(capital / own_per_share + 1e-9))
        qty = max(qty, 0)

    value = qty * price
    own = qty * own_per_share
    borrowed = value - own
    if rate is None:
        rate = slab_rate(borrowed)
    interest_day = borrowed * rate / 100 / 365
    return {
        "symbol": stock["symbol"],
        "price": price,
        "margin_pct": margin_pct,
        "qty": qty,
        "value": value,
        "own": own,
        "borrowed": borrowed,
        "rate": rate,
        "interest_day": interest_day,
        "interest_month": interest_day * 30,
    }


TOTAL_KEYS = ("qty", "value", "own", "borrowed", "interest_day", "interest_month")


def build_table(stocks, max_borrow, split=False, rate=None, capital=None):
    """Calculate every watchlist row.

    split=False -> each row is the max for that stock on its own.
    split=True  -> the limit (and capital) is shared equally, so the TOTAL
                   borrowed stays under max_borrow; the slab rate is then
                   decided by the total borrowed.
    Returns (rows, totals) -- totals is None when split is False.
    """
    n = len(stocks) if split else 1
    per_borrow = max_borrow / n
    per_capital = capital / n if capital is not None else None
    rows = [calculate(s, per_borrow, rate, per_capital) for s in stocks]
    if not split:
        return rows, None

    total_borrowed = sum(r["borrowed"] for r in rows)
    book_rate = rate if rate is not None else slab_rate(total_borrowed)
    rows = [calculate(s, per_borrow, book_rate, per_capital) for s in stocks]
    totals = {k: sum(r[k] for r in rows) for k in TOTAL_KEYS}
    totals["rate"] = book_rate
    return rows, totals


def inr(x):
    """Format a number the Indian way: 1,23,456.78"""
    neg = x < 0
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


COLUMNS = [
    ("Stock", "symbol", str),
    ("Price", "price", inr),
    ("Margin %", "margin_pct", lambda v: f"{v:g}%"),
    ("Max Qty", "qty", str),
    ("Trade Value", "value", inr),
    ("You Pay", "own", inr),
    ("Borrowed", "borrowed", inr),
    ("Rate % p.a.", "rate", lambda v: f"{v:.2f}%"),
    ("Interest/Day", "interest_day", inr),
    ("Interest/30d", "interest_month", inr),
]


def render_table(rows, total_row=None):
    """Plain-text table with no third-party dependencies."""
    body = [[fmt(r[key]) for _, key, fmt in COLUMNS] for r in rows]
    if total_row:
        body.append(total_row)
    headers = [h for h, _, _ in COLUMNS]
    widths = [max(len(h), *(len(r[i]) for r in body)) if body else len(h)
              for i, h in enumerate(headers)]

    def line(cells):
        out = []
        for i, c in enumerate(cells):
            out.append(c.ljust(widths[i]) if i == 0 else c.rjust(widths[i]))
        return "| " + " | ".join(out) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    out = [sep, line(headers), sep]
    for i, r in enumerate(body):
        if total_row and i == len(body) - 1:
            out.append(sep)
        out.append(line(r))
    out.append(sep)
    return "\n".join(out)


def save_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([h for h, _, _ in COLUMNS])
        for r in rows:
            w.writerow([r[key] if key == "symbol" else round(r[key], 2)
                        for _, key, _ in COLUMNS])


def amount_in_range(text):
    value = float(text)
    if not MIN_BORROW <= value <= MAX_BORROW:
        raise argparse.ArgumentTypeError(
            f"must be between {inr(MIN_BORROW)} (1 lakh) and {inr(MAX_BORROW)} (2 crore)")
    return value


def main(argv=None):
    p = argparse.ArgumentParser(description="MTF max-quantity calculator for your watchlist")
    p.add_argument("--watchlist", default=DEFAULT_WATCHLIST, help="path to watchlist CSV")
    p.add_argument("--max-borrow", type=amount_in_range, default=DEFAULT_MAX_BORROW,
                   help="max amount to borrow in INR, 100000 (1 lakh, default) to 20000000 (2 crore)")
    p.add_argument("--rate", type=float, default=None,
                   help="override the interest rate, %% p.a. (default: Pocketful slab rate)")
    p.add_argument("--capital", type=float, default=None,
                   help="your own cash available for margin, in INR (optional cap)")
    p.add_argument("--split", action="store_true",
                   help="split --max-borrow (and --capital) equally across all stocks "
                        "so the TOTAL borrowed stays under the limit")
    p.add_argument("--live", action="store_true", help="fetch live NSE prices via yfinance")
    p.add_argument("--csv", metavar="FILE", help="also save the results table to a CSV file")
    args = p.parse_args(argv)

    stocks = load_watchlist(args.watchlist)
    if not stocks:
        sys.exit(f"No stocks found in {args.watchlist}")
    if args.live:
        fetch_live_prices(stocks)

    rows, totals = build_table(stocks, args.max_borrow, args.split, args.rate, args.capital)
    n = len(stocks) if args.split else 1

    print("MTF Calculator Bot")
    print("  Interest rate : " + (f"{args.rate}% p.a. (override)" if args.rate is not None
                                  else "Pocketful slab rate for the borrowed amount"))
    print(f"  Max borrow    : Rs {inr(args.max_borrow)}"
          + (f"  (split -> Rs {inr(args.max_borrow / n)} per stock)" if args.split
             else "  (per stock)"))
    if args.capital is not None:
        print(f"  Own capital   : Rs {inr(args.capital)}"
              + (f"  (split -> Rs {inr(args.capital / n)} per stock)" if args.split else ""))
    print()

    total_row = None
    if totals:
        total_row = ["TOTAL", "", "", str(totals["qty"]), inr(totals["value"]),
                     inr(totals["own"]), inr(totals["borrowed"]), f"{totals['rate']:.2f}%",
                     inr(totals["interest_day"]), inr(totals["interest_month"])]
    print(render_table(rows, total_row))

    if not args.split:
        print("\nEach row is independent: it is the max you can buy of THAT stock alone.")
        print("Use --split to buy across all stocks with the total borrowed under the limit.")

    if args.csv:
        save_csv(rows, args.csv)
        print(f"\nSaved to {args.csv}")


if __name__ == "__main__":
    main()
