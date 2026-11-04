"""Stock history: candles with moving averages, the trend card, seasonality and how stocks compare."""

from datetime import timedelta

import streamlit as st

from tradingbots.core.runtime import MarketCalendar
from tradingbots.dashboard import charts, data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
settings, _ = cli.settings()
t = T.tokens()
st.title("Stock history")

metrics = {r["symbol"]: r for r in cli.table("history_metrics").to_dicts()}
bench = settings.benchmark()
# stocks first (the benchmark index last), then anything else the history bot has metrics for
symbols = list(dict.fromkeys([w.symbol for w in settings.stocks()] + sorted(metrics) +
                             ([bench.symbol] if bench else [])))
f1, f2, f3 = st.columns([2, 3, 2])
sym = f1.selectbox("Stock", symbols) if symbols else None
span = f2.segmented_control("Range", ["3M", "6M", "1Y", "2Y", "All"], default="1Y")
if f3.button("Refresh history now", help="asks the history bot to fetch the latest daily candles"):
    ui.result(cli.command("history", "refresh", {}, timeout=60), "History refreshed.")

if not sym:
    st.info("Add stocks to the watchlist on the Settings page.")
    st.stop()

m = metrics.get(sym)
if m:
    a, b, c, d, e = st.columns(5)
    a.metric("Trend", f"{T.arrow(m.get('trend_score'))} {str(m.get('trend') or '-').replace('_', ' ')}",
             f"score {m['trend_score']:+.0f}" if m.get("trend_score") is not None else None, delta_color="off")
    b.metric("1 month", T.pct(m.get("ret_1m")), f"1 year {T.pct(m.get('ret_1y'))}", delta_color="off")
    c.metric("RSI 14", f"{m['rsi14']:.0f}" if m.get("rsi14") is not None else "-",
             help="above 70: stretched after a rise; below 30: after a fall")
    d.metric("vs 50-day average", T.pct(m.get("dist_sma50")), f"vs 200-day {T.pct(m.get('dist_sma200'))}",
             delta_color="off")
    e.metric("From 52-week high", T.pct(-(m.get("from_high_52w") or 0)) if m.get("from_high_52w") is not None else "-",
             f"volatility {T.pct(m.get('vol_ann'), signed=False)} / yr", delta_color="off")
else:
    st.caption("No metrics for this stock yet - the history bot computes them after the first daily refresh.")

candles = cli.table("candles_daily", "symbol = ?", [sym], order="day")
if not candles.is_empty() and span and span != "All":
    days = {"3M": 92, "6M": 183, "1Y": 366, "2Y": 731}[span]
    last = candles["day"].max()
    # keep 200 extra days so the 200-day average is drawn from the first visible candle
    candles = candles.filter(candles["day"] >= last - timedelta(days=days + 300))
cal = MarketCalendar.from_file(cli.paths.holidays_file) if cli.paths.holidays_file.exists() else MarketCalendar()
fig = charts.price_chart(candles, sym, t, sorted(cal.holidays))
if not candles.is_empty() and span and span != "All":
    last = candles["day"].max()
    fig.update_xaxes(range=[last - timedelta(days={"3M": 92, "6M": 183, "1Y": 366, "2Y": 731}[span]), last])
ui.chart(fig)
st.caption("Blue candles closed higher than they opened, red lower. Lines: 50- and 200-day averages.")

left, right = st.columns([3, 2], gap="large")
with left:
    st.subheader("Month by month")
    mr = cli.table("monthly_returns", "symbol = ?", [sym], order="year, month")
    ui.chart(charts.monthly_heatmap(mr, t))
    with st.expander("Table view"):
        ui.table(mr.select(["year", "month", "ret"]).rename({"ret": "return %"}) if not mr.is_empty() else mr)
with right:
    st.subheader("Moves together")
    ui.chart(charts.corr_heatmap(cli.table("correlations"), t))
    st.caption("Correlation of daily returns over a year: 1 moves together, 0 unrelated, -1 opposite.")

st.subheader("All stocks compared")
cols = ["symbol", "close", "trend", "trend_score", "ret_1d", "ret_1m", "ret_3m", "ret_1y", "rsi14", "dist_sma50",
        "dist_sma200", "atr14_pct", "vol_ann", "max_dd_1y", "from_high_52w", "vol_ratio20", "beta_1y", "as_of"]
hm = cli.table("history_metrics", order="trend_score DESC")
ui.table(hm.select([c for c in cols if c in hm.columns]) if not hm.is_empty() else hm)
