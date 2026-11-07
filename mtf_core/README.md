# mtf_core

Reusable MTF (Margin Trading Facility) logic for any bot: how many shares of a stock you can
buy so the **borrowed amount stays under a limit**, what the interest costs on each broker, and
loading watchlists and live prices. No required dependencies (`yfinance` is optional, for live
prices).

## Install
From the repo root:
```bash
pip install -e .
```
Add live NSE prices as well:
```bash
pip install -e ".[live]"
```
From another project, straight from GitHub:
```bash
pip install "git+https://github.com/MSatishssj3/Amaha_QA_Automation.git"
```
Scripts inside this repo can also just `import mtf_core` when run from the repo root.

## Quick start
```python
from mtf_core import load_watchlist, plan, compare_platforms

stocks = load_watchlist("mtf_calculator_bot/watchlist.csv")
result = plan(stocks, max_borrow=1_00_000, platform="Pocketful")

for p in result.positions:
    print(p.symbol, p.qty, p.trade_value, p.own_margin, p.borrowed, p.interest_30d)

for c in compare_platforms(result.borrowed):          # cheapest first
    print(c.platform, c.rate, c.interest_30d)
```

## What's in it

| Function / class | What it does |
|---|---|
| `plan(stocks, max_borrow, platform=, split=, capital=, rate=)` | Max position for every stock. Returns a `Plan` with `.positions`, `.totals` (when `split=True`) and `.to_dict()`. |
| `max_position(stock, max_borrow, platform=, rate=, capital=)` | Same for one stock. Returns a `Position`. |
| `compare_platforms(borrowed)` | Interest on every platform for an amount, cheapest first (`PlatformCost`). |
| `MTFCalculator(platform=, max_borrow=, split=, capital=, price_fetcher=)` | Wrapper that remembers settings: `calc.plan(stocks)`, `calc.compare(plan)`. |
| `interest_rate(amount, platform)`, `next_slab(amount, platform)` | Slab lookups. |
| `register_platform(name, [(upto, rate), ..., (None, rate)])` | Add or replace a broker. |
| `load_rates(path)` | Load newer rates from a JSON file shaped like `rates.json`. |
| `load_watchlist(path)`, `save_watchlist(stocks, path)` | CSV with columns `symbol,price,margin_pct`. |
| `fetch_live_prices(symbols)` | Latest NSE close via yfinance → `{symbol: price}`. Raises `PriceFetchError` if yfinance is missing. |
| `with_live_prices(stocks, fetcher=)` | Copy of the stocks with fresh prices from any fetcher. |
| `inr()`, `rupees()`, `amount_in_words()`, `short_amount()` | `12,34,567.80`, `₹1,00,000.00`, `1.5 lakh`, `2.5 Cr`. |
| `Stock`, `Position`, `Totals`, `Plan`, `PlatformCost`, `Slab` | Plain frozen dataclasses. |

### Calculation
```
borrowed per share = price × (1 − margin_pct / 100)
max qty            = floor(max_borrow ÷ borrowed per share)    (and ≤ capital ÷ your share per share)
interest per day   = borrowed × rate ÷ 100 ÷ 365
```
With `split=True` the limit (and capital) is shared equally across the stocks, and the slab rate
is picked by the **total** borrowed.

## Using your own price source
A price fetcher is any function that takes a list of symbols and returns `{symbol: price}`.
For example, a broker API, a cache or a test stub:
```python
from mtf_core import MTFCalculator, load_watchlist

def my_prices(symbols):
    return {s: my_broker_api.ltp(s) for s in symbols}

calc = MTFCalculator(platform="Dhan", max_borrow=5_00_000, price_fetcher=my_prices)
result = calc.plan(load_watchlist("watchlist.csv"))
```

## Updating rates
The bundled rates are in `rates.json` (checked Nov 2026). Brokers change them often. Edit that
file, or call `load_rates("new_rates.json")` / `register_platform(...)` at runtime.
`mtf_calculator_bot/mtf_calculator.html` keeps its own copy for the browser.
`python -m unittest discover tests` fails if the two differ.
