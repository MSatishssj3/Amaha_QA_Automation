"""
MTF Calculator Bot
==================

For every stock in your watchlist, works out the maximum number of shares you
can buy using MTF (Margin Trading Facility) such that the amount BORROWED from
the broker stays under a chosen limit -- e.g. Pocketful charges 5.99% p.a. on
MTF borrowings below Rs 1 lakh, so you may want to keep the funded amount
below Rs 1,00,000.

How the numbers are calculated (per stock):
    trade value   = qty * price
    your margin   = trade value * margin_pct / 100       (you pay this)
    borrowed      = trade value - your margin            (broker funds this)
    max qty       = largest qty with borrowed <= max borrow
                    (and your margin <= --capital, if given)
    interest/day  = borrowed * rate / 100 / 365

Usage examples:
    python mtf_calculator.py
    python mtf_calculator.py --max-borrow 99999 --rate 5.99
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

# Pocketful MTF: 5.99% p.a. when the borrowed amount is below Rs 1 lakh.
DEFAULT_MAX_BORROW = 99_999.0
DEFAULT_RATE = 5.99


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


def calculate(stock, max_borrow, rate, capital=None):
    """Return the max-qty MTF position for one stock."""
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
    interest_day = borrowed * rate / 100 / 365
    return {
        "symbol": stock["symbol"],
        "price": price,
        "margin_pct": margin_pct,
        "qty": qty,
        "value": value,
        "own": own,
        "borrowed": borrowed,
        "interest_day": interest_day,
        "interest_month": interest_day * 30,
    }


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


def main(argv=None):
    p = argparse.ArgumentParser(description="MTF max-quantity calculator for your watchlist")
    p.add_argument("--watchlist", default=DEFAULT_WATCHLIST, help="path to watchlist CSV")
    p.add_argument("--max-borrow", type=float, default=DEFAULT_MAX_BORROW,
                   help="max amount you want to borrow, in INR (default 99999 = under 1 lakh)")
    p.add_argument("--rate", type=float, default=DEFAULT_RATE,
                   help="MTF interest rate, %% per annum (default 5.99)")
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

    n = len(stocks) if args.split else 1
    per_borrow = args.max_borrow / n
    per_capital = args.capital / n if args.capital is not None else None

    rows = [calculate(s, per_borrow, args.rate, per_capital) for s in stocks]

    print("MTF Calculator Bot")
    print(f"  Interest rate : {args.rate}% p.a.")
    print(f"  Max borrow    : Rs {inr(args.max_borrow)}"
          + (f"  (split -> Rs {inr(per_borrow)} per stock)" if args.split else "  (per stock)"))
    if args.capital is not None:
        print(f"  Own capital   : Rs {inr(args.capital)}"
              + (f"  (split -> Rs {inr(per_capital)} per stock)" if args.split else ""))
    print()

    total = None
    if args.split:
        t = {k: sum(r[k] for r in rows) for k in ("value", "own", "borrowed",
                                                  "interest_day", "interest_month")}
        total = ["TOTAL", "", "", str(sum(r["qty"] for r in rows)),
                 inr(t["value"]), inr(t["own"]), inr(t["borrowed"]),
                 inr(t["interest_day"]), inr(t["interest_month"])]
    print(render_table(rows, total))

    if not args.split:
        print("\nEach row is independent: it is the max you can buy of THAT stock alone.")
        print("Use --split to buy across all stocks with the total borrowed under the limit.")

    if args.csv:
        save_csv(rows, args.csv)
        print(f"\nSaved to {args.csv}")


if __name__ == "__main__":
    main()
