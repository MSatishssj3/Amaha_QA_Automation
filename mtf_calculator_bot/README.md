# MTF Calculator Bot

Shows, for every stock in your watchlist, the **maximum number of shares you can buy with MTF**
(Margin Trading Facility) so that the **amount borrowed from the broker stays under your limit**.

The limit is picked with a **slider from ₹1 lakh (default) to ₹2 crore**. ₹1 lakh keeps you in
Pocketful's cheapest slab (5.99% p.a.). It also **compares the interest on Pocketful, Dhan, Groww
and Zerodha** for the same amount.

## Interactive page (recommended)
Open **`mtf_calculator.html`** in any browser. Double-click it; nothing needs installing.

Everything updates **while you drag the slider**:

- **Amount to borrow via MTF**: a slider from ₹1 L to ₹2 Cr, starting at ₹1 L, with − / + buttons
  and quick picks (₹1 L, 5 L, 10 L, 25 L, 50 L, 1 Cr, 2 Cr). Orange marks on the slider show where
  the selected platform's interest rate jumps.
- **Interest rate now** (beside the slider): the rate you'd pay at the current amount, with the
  interest per day. It turns green, amber or red as the rate rises.
- **Interest for 30 days, by platform**: Pocketful, Dhan, Groww and Zerodha side by side, cheapest
  marked. Click one to use its rates in the table.
- **Each stock on its own / Whole watchlist together**: either the max of each stock by itself,
  or the amount split across all stocks so the total stays under the limit (with a Total row).
- **Watchlist table**: for every stock, the **shares you can buy** and the **total amount**, plus
  what you pay, what you borrow and the interest. Edit prices and margin % in the table, add or
  remove stocks, or import `watchlist.csv`. Changes are kept in your browser.

## Command line
```bash
python mtf_calculator.py                          # each stock on its own, borrowed ≤ 1 lakh
python mtf_calculator.py --split                  # whole watchlist together, TOTAL borrowed ≤ 1 lakh
python mtf_calculator.py --max-borrow 500000      # any amount from 100000 (1 L) to 20000000 (2 Cr)
python mtf_calculator.py --capital 30000          # also cap by your own cash
python mtf_calculator.py --platform Dhan          # use Dhan / Groww / Zerodha rates in the table
python mtf_calculator.py --rate 9.99              # override the slab rate
python mtf_calculator.py --live                   # live NSE prices (pip install yfinance)
python mtf_calculator.py --csv results.csv        # save the table
```

## Watchlist
`watchlist.csv` has one row per stock:

| column       | meaning                                                         |
|--------------|-----------------------------------------------------------------|
| `symbol`     | NSE symbol, e.g. `RELIANCE`                                     |
| `price`      | current price (₹). Ignored when live prices are on              |
| `margin_pct` | % of trade value **you** pay upfront (see the MTF margin in your app) |

## Formula
```
borrowed per share = price × (1 − margin_pct/100)
max qty            = floor(limit ÷ borrowed per share)   (and ≤ capital ÷ your margin per share)
interest per day   = borrowed × slab rate ÷ 100 ÷ 365
```

## Interest rates by platform
| Platform  | Borrowed amount → rate (p.a.)                                                    |
|-----------|----------------------------------------------------------------------------------|
| Pocketful | up to ₹1 L: 5.99% · ₹1 L–₹25 L: 14.60% · above ₹25 L: 16.00%                      |
| Dhan      | up to ₹5 L: 12.49% · ₹5 L–₹10 L: 13.49% · ₹10 L–₹25 L: 14.49% · above ₹25 L: 15.49% |
| Groww     | 14.95% on any amount (0.041%/day)                                                |
| Zerodha   | 14.60% on any amount (0.04%/day)                                                 |

Where a platform has slabs, the slab is picked by the total borrowed and that rate applies to the
whole amount. The maximum quantity is the same on every platform when the stock's margin % is the
same; only the interest changes. Each broker can ask a different margin % for the same stock.
The rates are set in two places: `PLATFORMS` in `mtf_calculator.py` and in `mtf_calculator.html`.

These rates were taken from the brokers' published pricing in Nov 2026 and change often. Check them
in each app and edit both places if they differ. Interest shown excludes
brokerage, GST and pledge charges.
