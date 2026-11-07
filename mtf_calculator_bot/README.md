# MTF Calculator Bot

Shows, for every stock in your watchlist, the **maximum number of shares you can buy with MTF**
(Margin Trading Facility) so that the **amount borrowed from the broker stays under your limit**.

The limit is picked with a **slider from ₹1 lakh (default) to ₹2 crore**. ₹1 lakh keeps you in
Pocketful's cheapest slab (5.99% p.a.).

## Slider app (recommended)
```bash
pip install -r requirements.txt
streamlit run app.py
```
This opens in your browser:

- **Maximum amount to borrow**: a slider from ₹1 L to ₹2 Cr. The steps are ₹10,000 up to ₹10 L,
  ₹1 L up to ₹1 Cr, then ₹5 L up to ₹2 Cr. Click the slider and use the arrow keys to move one
  step at a time.
- **Spread the limit across the whole watchlist**: when this is off, each row is the most you can
  buy of that stock on its own. When it is on, the limit is shared equally across all stocks so
  the **total** borrowed stays under it, and a TOTAL row is shown.
- **Use live NSE prices**: fetches current prices with `yfinance`.
- **Watchlist**: edit stocks, prices and margin % in the app and click *Save watchlist*.

## Command line
```bash
python mtf_calculator.py                          # each stock on its own, borrowed ≤ 1 lakh
python mtf_calculator.py --split                  # whole watchlist together, TOTAL borrowed ≤ 1 lakh
python mtf_calculator.py --max-borrow 500000      # any amount from 100000 (1 L) to 20000000 (2 Cr)
python mtf_calculator.py --capital 30000          # also cap by your own cash
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

## Interest slabs (Pocketful)
| Borrowed amount          | Rate       |
|--------------------------|------------|
| Up to ₹1,00,000          | 5.99% p.a. |
| ₹1,00,001 to ₹25,00,000  | 14.60% p.a.|
| Above ₹25,00,000         | 16.00% p.a.|

The slab is picked by the total borrowed, and that rate applies to the whole amount. These figures
come from Pocketful's MTF calculator page and may change, so check them in the Pocketful app and
edit `INTEREST_SLABS` in `mtf_calculator.py` if needed. Interest shown excludes GST and pledge
charges.
