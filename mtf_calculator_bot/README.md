# MTF Calculator Bot

Shows, for every stock in your watchlist, the **maximum number of shares you can buy with MTF**
(Margin Trading Facility) so that the **amount borrowed from the broker stays under a limit**.
Example: Pocketful charges **5.99% p.a.** on MTF borrowings **below ₹1 lakh**, so the default
limit is ₹99,999 at 5.99%.

## Setup
Edit `watchlist.csv`, one row per stock:

| column       | meaning                                                         |
|--------------|-----------------------------------------------------------------|
| `symbol`     | NSE symbol, e.g. `RELIANCE`                                     |
| `price`      | current price (₹). Ignored with `--live`                        |
| `margin_pct` | % of trade value **you** pay upfront (see the MTF margin in your app) |

## Run
```bash
python mtf_calculator.py                          # each stock on its own, borrowed < 1 lakh
python mtf_calculator.py --split                  # whole watchlist together, TOTAL borrowed < 1 lakh
python mtf_calculator.py --capital 30000          # also cap by your own cash
python mtf_calculator.py --max-borrow 200000 --rate 9.99   # other limit / rate
python mtf_calculator.py --live                   # live NSE prices (pip install yfinance)
python mtf_calculator.py --csv results.csv        # save the table
```

## Formula
```
borrowed per share = price × (1 − margin_pct/100)
max qty            = floor(max_borrow ÷ borrowed per share)   (and ≤ capital ÷ your margin per share)
interest per day   = borrowed × rate ÷ 100 ÷ 365
```
Interest shown excludes GST/pledge charges; confirm current rates and slabs in the Pocketful app.
