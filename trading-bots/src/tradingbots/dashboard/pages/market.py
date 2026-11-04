"""Live market: the watchlist's latest quotes and today's 1-minute chart."""

import streamlit as st

from tradingbots.core import topics
from tradingbots.dashboard import charts, data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
settings, _ = cli.settings()
st.title("Live market")

symbols = [w.symbol for w in settings.watchlist]
pick = st.selectbox("Chart", symbols, index=min(1, len(symbols) - 1) if symbols else 0)


@st.fragment(run_every=max(1.0, settings.dashboard.refresh_s))
def live() -> None:
    t = T.tokens()
    now = ui.team_now(cli)
    status = (cli.recent(topics.MARKET_STATUS, 1) or [{}])[0]
    q = cli.kv(topics.KV_QUOTES) or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("Feed", "connected" if status.get("connected") else "not connected",
              help=f"platform {status.get('platform', settings.platform.name)}")
    c2.metric("Instruments streaming", status.get("instruments", 0))
    age = status.get("last_tick_age_s")
    c3.metric("Last tick", f"{age:.0f}s ago" if age is not None else "-")
    rows = []
    for v in sorted(q.values(), key=lambda r: r.get("symbol", "")):
        ch = v.get("change_pct")
        rows.append({"Symbol": v.get("symbol"), "LTP": v.get("ltp"),
                     "Change": f"{T.arrow(ch)} {T.pct(ch, nd=2)}" if ch is not None else "-",
                     "Bid": v.get("bid") or None, "Ask": v.get("ask") or None, "Volume": v.get("volume"),
                     "Prev close": v.get("prev_close"),
                     "Updated": f"{max(0, now - float(v['updated_ts'])):.0f}s ago" if v.get("updated_ts") else "-"})
    ui.table(rows, column_config={"LTP": st.column_config.NumberColumn(format="%.2f"),
                                  "Bid": st.column_config.NumberColumn(format="%.2f"),
                                  "Ask": st.column_config.NumberColumn(format="%.2f"),
                                  "Prev close": st.column_config.NumberColumn(format="%.2f"),
                                  "Volume": st.column_config.NumberColumn(format="localized")})
    if pick:
        st.subheader(f"{pick} today (1-minute closes)")
        today = ui.team_today(cli)
        bars = cli.table("bars_1m", "date = ? AND symbol = ?", [today, pick], order="start_ts")
        ui.chart(charts.intraday_line(bars, pick, t))


live()
