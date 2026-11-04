"""Money: where it is, what it earned, what it cost (charges, expenses) and the cash moving in and out."""

from datetime import date, timedelta

import streamlit as st

from tradingbots.dashboard import charts, data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
settings, _ = cli.settings()
t = T.tokens()
st.title("Money")

ms = {r["metric"]: r.get("value") for r in cli.table("money_summary").to_dicts()}
k = st.columns(5)
k[0].metric("Net worth", T.inr(ms.get("net_worth"), compact=True), help="holdings + positions at market + cash")
k[1].metric("Invested", T.inr(ms.get("invested"), compact=True))
up = ms.get("unrealized_pnl")
k[2].metric("Unrealised P&L", f"{T.arrow(up)} {T.inr(up, signed=True)}" if up is not None else "-")
k[3].metric("Charges this month", T.inr(ms.get("charges_mtd")), f"{T.inr(ms.get('charges_ytd'))} this year",
            delta_color="off", delta_arrow="off")
x = ms.get("xirr")
k[4].metric("XIRR", T.pct(x) if x is not None else "-", help="annualised return on the money you deposited")

left, right = st.columns(2, gap="large")
alloc = cli.query("SELECT symbol, SUM(current_value) AS value, SUM(invested) AS invested, SUM(pnl) AS pnl "
                  "FROM money_allocation WHERE symbol IS NOT NULL GROUP BY symbol ORDER BY value DESC")
with left:
    st.subheader("Where the money is")
    if alloc.is_empty():
        ui.chart(charts.empty(t, "No holdings or positions yet"))
    else:
        ui.chart(charts.ranked_bars(alloc["symbol"].to_list(), alloc["value"].to_list(), t))
    byp = cli.query("SELECT platform, SUM(current_value) AS value FROM money_allocation GROUP BY platform")
    if byp.height > 1:
        st.caption("By platform: " + " · ".join(f"{r['platform']} {T.inr(r['value'])}" for r in byp.to_dicts()))
with right:
    st.subheader("Profit and loss by day")
    since = date.today() - timedelta(days=45)
    pnl = cli.query("SELECT day, SUM(realized) AS realized, SUM(net) AS net FROM money_pnl WHERE day >= ? "
                    "GROUP BY day ORDER BY day", [since])
    ui.chart(charts.diverging_bars([f"{d:%d %b}" for d in pnl["day"].to_list()] if not pnl.is_empty() else [],
                                   pnl["net"].to_list() if not pnl.is_empty() else [], t))
    st.caption("Realised profit or loss per day, after charges. Blue above zero, red below.")

st.subheader("Charges")
c1, c2 = st.columns(2, gap="large")
monthly = cli.query("SELECT month, SUM(total) AS total, SUM(turnover) AS turnover FROM money_charges "
                    "GROUP BY month ORDER BY month")
with c1:
    months = [date.fromisoformat(f"{m}-01").strftime("%b %Y") for m in monthly["month"].to_list()] \
        if not monthly.is_empty() else []
    ui.chart(charts.columns(months, monthly["total"].to_list() if not monthly.is_empty() else [], t))
    st.caption("All charges per month: brokerage, STT, exchange fees, SEBI fee, stamp duty and GST.")
by_type = cli.query("SELECT SUM(brokerage) AS brokerage, SUM(stt) AS stt, SUM(exchange_txn) AS exchange, "
                    "SUM(sebi) AS sebi, SUM(stamp_duty) AS stamp, SUM(gst) AS gst FROM money_charges "
                    "WHERE month >= ?", [f"{date.today():%Y}-01"])
with c2:
    if by_type.is_empty() or by_type.row(0)[0] is None:
        ui.chart(charts.empty(t, "No charges this year yet"))
    else:
        r = by_type.to_dicts()[0]
        names = {"brokerage": "Brokerage", "stt": "STT", "exchange": "Exchange fees", "sebi": "SEBI fee",
                 "stamp": "Stamp duty", "gst": "GST"}
        ui.chart(charts.ranked_bars([names[k] for k in r], [r[k] or 0 for k in r], t))
        st.caption("This year's charges by type.")
with st.expander("Charges by stock and month (table)"):
    ui.table(cli.table("money_charges", order="month DESC, total DESC"))

st.subheader("Money in and out")
ui.table(cli.table("money_cashflows", order="month DESC"))

st.subheader("Other expenses")
st.caption("Spending outside trades, such as a data plan, a server or the AI API. AI cost is added automatically.")
exp = cli.table("money_expenses", order="day DESC")
ui.table(exp)
with st.form("add_expense", clear_on_submit=True):
    a, b, c, d = st.columns([2, 2, 2, 3])
    day = a.date_input("Day", value=ui.team_today(cli))
    amount = b.number_input("Amount (₹)", min_value=0.0, step=100.0)
    cats = list(dict.fromkeys(settings.money.expense_categories + ["other"]))
    cat = c.selectbox("Category", cats)
    note = d.text_input("Note")
    if st.form_submit_button("Add expense"):
        ui.result(cli.command("money", "add_expense", {"day": day.isoformat(), "amount": amount, "category": cat,
                                                       "note": note, "platform": settings.platform.name}),
                  "Expense added - the money tables update in a few seconds.")
manual = [e for e in (cli.command("money", "list_expenses").get("expenses") or [])]
if manual:
    with st.expander("Delete an expense"):
        opts = {f"{e['day']} · {e['category']} · {T.inr(e['amount'])} · {e.get('note') or ''}": e["id"] for e in manual}
        sel = st.selectbox("Expense", list(opts))
        if st.button("Delete", type="secondary"):
            ui.result(cli.command("money", "delete_expense", {"id": opts[sel]}), "Deleted.")
