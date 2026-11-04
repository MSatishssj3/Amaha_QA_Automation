"""Performance: how your trading is going (win rate, expectancy, drawdown, charges and slippage), when and where
you make or lose money, and a journal of every trade with your own tags and notes."""

import streamlit as st

from tradingbots.bots.journal import analytics as A
from tradingbots.dashboard import charts, data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
t = T.tokens()
st.title("Performance")
st.caption("Each trade is one round trip, from flat back to flat. Figures are after charges and count on the day "
           "the trade closed. Decision support, not advice.")

trips = cli.table("perf_trades").to_dicts()
if not trips:
    st.info("No trades yet. The journal fills in as the trading engine or you trade.")
    st.stop()

a, b = st.columns([3, 2])
periods = {"Today": "today", "This month": "month", "This financial year": "fy", "All time": "all"}
books = {"Real money": "real", "Engine": "bot", "Manual": "manual", "Paper": "paper"}
period = a.radio("Period", list(periods), index=1, horizontal=True)
book = b.radio("Trades", list(books), horizontal=True,
               help="Real money = the engine's and your own trades; Paper = the engine's simulated fills")
today = ui.team_today(cli)
sel = [x for x in A.of_source(trips, books[book]) if A.in_scope(x, periods[period], today)]
s = A.stats(sel)
if books[book] == "paper":
    st.caption("Paper trades: simulated fills on live prices, charges estimated by the engine - no real money.")

k = st.columns(4)
net = s["net_pnl"]
k[0].metric("Net P&L", f"{T.arrow(net)} {T.inr(net, signed=True)}",
            f"gross {T.inr(s['gross_pnl'], signed=True)} · charges {T.inr(s['charges'])}", delta_color="off",
            delta_arrow="off")
k[1].metric("Closed trades", s["trades"], f"{T.plural(s['wins'], 'win')} · {T.plural(s['losses'], 'loss', 'losses')}",
            delta_color="off", delta_arrow="off")
k[2].metric("Win rate", T.pct(s["win_rate"], signed=False) if s["trades"] else "-",
            f"avg win {T.inr(s['avg_win'])} · avg loss {T.inr(s['avg_loss'])}" if s["trades"] else None,
            delta_color="off", delta_arrow="off", help="a win is a trade with net P&L above zero, after charges")
k[3].metric("Expectancy", T.inr(s["expectancy"], signed=True) if s["trades"] else "-",
            f"wins are {s['payoff']:.2f}x the losses" if s["payoff"] else None, delta_color="off", delta_arrow="off",
            help="average net P&L per trade")
k = st.columns(4)
pf = s["profit_factor"]
k[0].metric("Profit factor", f"{pf:.2f}" if pf is not None else ("no losses" if s["wins"] else "-"),
            help="money won on winning trades / money lost on losing trades; above 1 is profitable")
k[1].metric("Max drawdown", T.inr(s["max_drawdown"]) if s["trades"] else "-",
            help="largest fall of the cumulative net P&L from a high, in this period")
k[2].metric("Longest losing streak", T.plural(s["losing_streak"], "trade") if s["trades"] else "-",
            f"longest winning streak {s['winning_streak']}" if s["trades"] else None, delta_color="off",
            delta_arrow="off")
cp = s["charges_pct_of_gross"]
k[3].metric("Charges vs gross profit", T.pct(cp, signed=False) if cp is not None else "-",
            f"engine slippage {T.inr(s['slippage'], signed=True)}" if s["slippage"] is not None else None,
            delta_color="off", delta_arrow="off",
            help="charges as a share of the gross P&L; slippage = fills against the price the engine expected "
                 "(+ means it cost you)")

st.subheader("Equity curve")
ui.chart(charts.equity_curve(A.equity_curve(sel), t))
st.caption("Cumulative net P&L after each closed trade in the period.")

left, right = st.columns(2, gap="large")
with left:
    st.subheader("By hour of entry")
    hb = A.breakdown(sel, "hour")
    ui.chart(charts.diverging_bars([r["bucket"] for r in hb], [r["net_pnl"] for r in hb], t))
with right:
    st.subheader("By weekday")
    wb = A.breakdown(sel, "weekday")
    ui.chart(charts.diverging_bars([r["bucket"] for r in wb], [r["net_pnl"] for r in wb], t))
st.caption("Net P&L of the trades entered in each hour (IST) and on each weekday. Blue above zero, red below.")

st.subheader("By stock")
sb = A.breakdown(sel, "symbol")
ui.chart(charts.diverging_bars([r["bucket"] for r in sb[:20]], [r["net_pnl"] for r in sb[:20]], t))


def table_of(dim: str, label: str) -> None:
    ui.table([{label: r["bucket"], "Trades": r["trades"], "Win rate": T.pct(r["win_rate"], signed=False),
               "Net P&L": T.inr(r["net_pnl"], signed=True), "Per trade": T.inr(r["avg_net"], signed=True),
               "Charges": T.inr(r["charges"])} for r in A.breakdown(sel, dim)])


c1, c2, c3 = st.columns(3, gap="large")
with c1:
    st.markdown("**How trades ended**")
    table_of("exit", "Ended by")
with c2:
    st.markdown("**Holding time**")
    table_of("holding", "Held")
with c3:
    st.markdown("**Your tags**")
    table_of("tag", "Tag")

st.subheader("Trade journal")
st.caption("Add tags (comma separated, e.g. breakout, news, revenge) and notes, then save. Tags get their own "
           "breakdown above.")
rows = sorted((x for x in sel if x["status"] == "closed"), key=lambda x: x["exit_ts"] or 0, reverse=True)[:300]
view = [{"key": x["trade_key"], "Closed": ui.hhmm(x["exit_ts"]), "Stock": x["symbol"], "Side": x["direction"],
         "Qty": x["qty"], "Entry": x["avg_entry"], "Exit": x["avg_exit"], "Net P&L": x["net"], "Charges": x["charges"],
         "Held": T.duration(x["holding_s"]), "Ended by": x["exit_reason"] or "", "Source": x["source"],
         "Tags": x["tags"] or "", "Note": x["note"] or ""} for x in rows]
if view:
    edited = st.data_editor(view, hide_index=True, width="stretch", key="journal",
                            disabled=[c for c in view[0] if c not in ("Tags", "Note")],
                            column_config={"key": None,
                                           "Net P&L": st.column_config.NumberColumn(format="₹%.2f"),
                                           "Charges": st.column_config.NumberColumn(format="₹%.2f"),
                                           "Tags": st.column_config.TextColumn(max_chars=200),
                                           "Note": st.column_config.TextColumn(width="large", max_chars=2000)})
    if st.button("Save tags and notes", type="primary"):
        before = {v["key"]: (v["Tags"], v["Note"]) for v in view}
        changed = [{"trade_key": e["key"], "tags": e["Tags"] or "", "note": e["Note"] or ""} for e in edited
                   if (e["Tags"] or "", e["Note"] or "") != before.get(e["key"])]
        if changed:
            ui.result(cli.command("journal", "annotate", {"items": changed}), f"Saved {T.plural(len(changed), 'trade')}.")
        else:
            st.info("Nothing changed.")
else:
    st.caption("No closed trades in this period.")

opened = [x for x in A.of_source(trips, books[book]) if x["status"] == "open"]
if opened:
    with st.expander(f"Open trades ({len(opened)})"):
        ui.table([{"Since": ui.hhmm(x["entry_ts"]), "Stock": x["symbol"], "Side": x["direction"], "Open qty": x["open_qty"],
                   "Avg entry": x["avg_entry"], "Product": x["product"], "Source": x["source"]} for x in opened])
odd = [x for x in A.of_source(trips, books[book]) if x["status"] == "unmatched"]
if odd:
    syms = ", ".join(sorted({x["symbol"] for x in odd}))
    st.caption(f"Left out: {T.plural(len(odd), 'sale')} of shares bought before the trade history, with no known "
               f"cost ({syms}).")
