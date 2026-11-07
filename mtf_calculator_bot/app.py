"""
MTF Calculator Bot -- slider UI
===============================

Pick the maximum amount you want to borrow with a slider (Rs 1 lakh to Rs 2 crore,
default Rs 1 lakh) and see, for every stock in your watchlist, how many shares you
can buy with MTF without the borrowed amount going over that limit.

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

import os

import pandas as pd
import streamlit as st

from mtf_calculator import (
    DEFAULT_MAX_BORROW,
    DEFAULT_WATCHLIST,
    INTEREST_SLABS,
    MAX_BORROW,
    MIN_BORROW,
    build_table,
    fetch_live_prices,
    inr,
    load_watchlist,
)


def slider_steps():
    """Slider stops: fine near 1 lakh, coarser as the amount grows.

    1 L  -> 10 L  in Rs 10,000 steps
    10 L -> 1 Cr  in Rs 1,00,000 steps
    1 Cr -> 2 Cr  in Rs 5,00,000 steps
    """
    steps = list(range(int(MIN_BORROW), 10_00_000, 10_000))
    steps += list(range(10_00_000, 1_00_00_000, 1_00_000))
    steps += list(range(1_00_00_000, int(MAX_BORROW) + 1, 5_00_000))
    return steps


def short_inr(amount):
    """1,00,000 -> 'Rs 1 L', 1,50,00,000 -> 'Rs 1.5 Cr'."""
    if amount >= 1_00_00_000:
        return f"₹{amount / 1_00_00_000:g} Cr"
    return f"₹{amount / 1_00_000:g} L"


def rupees(amount):
    return "₹" + inr(amount)


@st.cache_data(ttl=300, show_spinner="Fetching live NSE prices...")
def live_prices(symbols):
    stocks = [{"symbol": s, "price": 0.0, "margin_pct": 0.0} for s in symbols]
    fetch_live_prices(stocks)
    return {s["symbol"]: s["price"] for s in stocks}


st.set_page_config(page_title="MTF Calculator Bot", page_icon="📈", layout="wide")
st.title("📈 MTF Calculator Bot")
st.caption("How many shares of each watchlist stock you can buy with MTF "
           "while keeping the borrowed amount under your limit.")

# ---------------------------------------------------------------- controls
max_borrow = st.select_slider(
    "Maximum amount to borrow",
    options=slider_steps(),
    value=int(DEFAULT_MAX_BORROW),
    format_func=short_inr,
    help="Default ₹1 lakh keeps you in Pocketful's 5.99% slab. Drag up to ₹2 crore. "
         "Click it and use the arrow keys to move one step at a time.",
)

c1, c2 = st.columns(2)
split = c1.toggle(
    "Spread the limit across the whole watchlist",
    help="Off: each row is the max for that stock on its own.\n\n"
         "On: the limit is shared equally so the TOTAL borrowed stays under it.",
)
use_live = c2.toggle("Use live NSE prices (needs yfinance)")

# ---------------------------------------------------------------- watchlist
with st.expander("Watchlist (edit prices / margin %, then save)"):
    wl = pd.DataFrame(load_watchlist(DEFAULT_WATCHLIST),
                      columns=["symbol", "price", "margin_pct"])
    edited = st.data_editor(
        wl,
        num_rows="dynamic",
        width="stretch",
        column_config={
            "symbol": st.column_config.TextColumn("Stock"),
            "price": st.column_config.NumberColumn("Price (₹)", min_value=0.0, format="%.2f"),
            "margin_pct": st.column_config.NumberColumn(
                "Margin %", min_value=0.0, max_value=99.99, format="%.2f",
                help="% of the trade value you pay upfront (see MTF margin in the app)"),
        },
        key="watchlist",
    )
    if st.button("Save watchlist"):
        with open(DEFAULT_WATCHLIST, "w", encoding="utf-8", newline="") as fh:
            fh.write("# price      -> current price per share in INR\n"
                     "# margin_pct -> % of the trade value YOU pay upfront for MTF\n")
            edited.dropna(subset=["symbol"]).to_csv(fh, index=False)
        st.success(f"Saved to {os.path.basename(DEFAULT_WATCHLIST)}")


def num(v):
    return 0.0 if pd.isna(v) else float(v)


stocks = [
    {"symbol": str(r.symbol).strip().upper(),
     "price": num(r.price),
     "margin_pct": num(r.margin_pct)}
    for r in edited.itertuples()
    if isinstance(r.symbol, str) and r.symbol.strip()
]
if not stocks:
    st.warning("Add at least one stock to the watchlist.")
    st.stop()

if use_live:
    prices = live_prices(tuple(s["symbol"] for s in stocks))
    if not any(prices.values()):
        st.warning("Couldn't fetch live prices (run `pip install yfinance` and check your "
                   "internet connection). Using the watchlist prices instead.")
    for s in stocks:
        if prices.get(s["symbol"]):
            s["price"] = prices[s["symbol"]]

# ---------------------------------------------------------------- results
rows, totals = build_table(stocks, float(max_borrow), split=split)

if totals:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Borrow limit (total)", rupees(max_borrow))
    m2.metric("Total borrowed", rupees(totals["borrowed"]))
    m3.metric("Interest rate", f"{totals['rate']:.2f}% p.a.")
    m4.metric("Interest per day", rupees(totals["interest_day"]))
else:
    m1, m2, m3 = st.columns(3)
    m1.metric("Borrow limit (per stock)", rupees(max_borrow))
    m2.metric("Stocks in watchlist", len(rows))
    m3.metric("Interest rate",
              " / ".join(f"{r:.2f}%" for r in sorted({r["rate"] for r in rows})) + " p.a.")

table = pd.DataFrame(rows).rename(columns={
    "symbol": "Stock", "price": "Price (₹)", "margin_pct": "Margin %",
    "qty": "Max Qty", "value": "Trade Value (₹)", "own": "You Pay (₹)",
    "borrowed": "Borrowed (₹)", "rate": "Rate % p.a.",
    "interest_day": "Interest/Day (₹)", "interest_month": "Interest/30d (₹)",
})
if totals:
    table.loc[len(table)] = {
        "Stock": "TOTAL", "Max Qty": totals["qty"], "Trade Value (₹)": totals["value"],
        "You Pay (₹)": totals["own"], "Borrowed (₹)": totals["borrowed"],
        "Rate % p.a.": totals["rate"], "Interest/Day (₹)": totals["interest_day"],
        "Interest/30d (₹)": totals["interest_month"],
    }

money_cols = ["Price (₹)", "Trade Value (₹)", "You Pay (₹)", "Borrowed (₹)",
              "Interest/Day (₹)", "Interest/30d (₹)"]
styled = table.style.format(
    {**{c: lambda v: "" if pd.isna(v) else inr(v) for c in money_cols},
     "Margin %": lambda v: "" if pd.isna(v) else f"{v:g}%",
     "Rate % p.a.": lambda v: f"{v:.2f}%",
     "Max Qty": lambda v: f"{int(v)}"},
    na_rep="",
)
styled = styled.hide(axis="index").set_properties(
    subset=[c for c in table.columns if c != "Stock"], **{"text-align": "right"})
if totals:
    styled = styled.apply(
        lambda r: ["font-weight: bold" if r["Stock"] == "TOTAL" else "" for _ in r], axis=1)

st.table(styled)

st.caption(
    ("The limit is shared equally across all stocks, so the total borrowed stays under it."
     if split else
     "Each row is independent: the max you can buy of that stock on its own. "
     "Turn on “Spread the limit” to buy the whole watchlist together.")
)

with st.expander("Interest slabs used"):
    whole = lambda v: "₹" + inr(v)[:-3]
    lines, lower = [], 0
    for upto, rate in INTEREST_SLABS:
        if lower == 0:
            label = f"Up to {whole(upto)}"
        elif upto == float("inf"):
            label = f"Above {whole(lower)}"
        else:
            label = f"{whole(lower + 1)} to {whole(upto)}"
        lines.append(f"- {label}: **{rate:.2f}% p.a.**")
        lower = upto
    st.markdown("\n".join(lines))
    st.caption("The slab is picked by the total borrowed and that rate applies to the whole "
               "amount. Interest excludes GST and pledge charges. Check the current rates in "
               "the Pocketful app and edit INTEREST_SLABS in mtf_calculator.py if they change.")
