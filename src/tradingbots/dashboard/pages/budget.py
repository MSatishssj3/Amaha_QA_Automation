"""Daily budget: today's limits against what has been used (by the trading engine and by you)."""

import streamlit as st

from tradingbots.core import topics
from tradingbots.dashboard import data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
settings, _ = cli.settings()
b = settings.budget
st.title("Daily budget")
st.caption("The trading engine checks these limits before every new entry. Exits are never blocked. Trades you "
           "place yourself in the Dhan app count too" + ("" if b.count_manual_trades else " (switched off)") + ".")


@st.fragment(run_every=settings.dashboard.refresh_s)
def live() -> None:
    t = T.tokens()
    u = cli.kv(topics.KV_BUDGET_USAGE) or {}
    if not b.enabled:
        st.warning("The daily budget is switched off (budget.enabled = false). The engine's own risk limits still apply.")
    if not u:
        st.info("No usage reported yet - start the budget bot (`bots run --all`).")
        return
    k = st.columns(4)
    k[0].metric("New entries today", T.inr(u["total"]["turnover_inr"]), f"{u['total']['entries']} entries",
                delta_color="off")
    k[1].metric("By the trading engine", T.inr(u["bot"]["turnover_inr"]), f"{u['bot']['entries']} entries",
                delta_color="off")
    k[2].metric("By you (Dhan app)", T.inr(u["external"]["turnover_inr"]), f"{u['external']['trades']} orders",
                delta_color="off")
    k[3].metric("Entries blocked by the budget", u.get("budget_rejects", 0))
    warn = (b.alert_at_pct or [80])[0]
    for it in u.get("limits", []):
        used = f"{it['used']:.0f} of {it['limit']:.0f}" if it["unit"] == "count" else \
            f"{T.inr(it['used'])} of {T.inr(it['limit'])}"
        note = f"{T.inr(it['remaining'])} left" if it["unit"] != "count" else f"{it['remaining']:.0f} left"
        st.markdown(T.meter_html(it["label"], used, it["pct"], t, warn_at=warn, note=note), unsafe_allow_html=True)
    if u.get("rejects"):
        with st.expander("Entries blocked today, by rule"):
            ui.table([{"Rule": k, "Blocked": v} for k, v in sorted(u["rejects"].items())])


live()

st.subheader("Change the limits")
st.caption("Saved through the config bot, which validates and versions every change. The engine applies new limits "
           "within seconds. 0 means no limit for that rule.")
with st.form("limits"):
    c = st.columns(3)
    vals = {
        "daily_capital_limit_inr": c[0].number_input("New entries per day (₹)", 0.0, value=b.daily_capital_limit_inr,
                                                     step=5000.0),
        "max_capital_in_use_inr": c[1].number_input("Capital in use at any moment (₹)", 0.0,
                                                    value=b.max_capital_in_use_inr, step=5000.0),
        "per_stock_daily_limit_inr": c[2].number_input("Per stock per day (₹)", 0.0, value=b.per_stock_daily_limit_inr,
                                                       step=5000.0),
        "max_trades_per_day": c[0].number_input("Entries per day", 0, value=b.max_trades_per_day, step=1),
        "max_trades_per_stock": c[1].number_input("Entries per stock per day", 0, value=b.max_trades_per_stock, step=1),
        "max_daily_loss_inr": c[2].number_input("Daily loss limit (₹) - halts trading", 0.0, value=b.max_daily_loss_inr,
                                                step=500.0),
    }
    e1, e2 = st.columns(2)
    vals["enabled"] = e1.toggle("Budget enabled", value=b.enabled)
    vals["count_manual_trades"] = e2.toggle("Count my own trades from the Dhan app", value=b.count_manual_trades)
    if st.form_submit_button("Save limits", type="primary"):
        ui.result(cli.command("config", "update", {"patch": {"budget": vals}, "author": "dashboard",
                                                   "note": "daily budget page"}), "Limits saved.")

st.subheader("History")
h = cli.command("budget", "history", {"days": 30})
if h.get("ok"):
    days = [{"Day": d["date"], "Entries (₹)": d["total"]["turnover_inr"], "Engine (₹)": d["bot"]["turnover_inr"],
             "Yours (₹)": d["external"]["turnover_inr"], "Entries": d["total"]["entries"],
             "Blocked": d.get("budget_rejects", 0), "Engine P&L (₹)": d.get("net_pnl_inr")} for d in h.get("days", [])]
    ui.table(days)
    alerts = [a for a in cli.recent(topics.ALERT, 200) if str(a.get("key", "")).startswith("budget")]
    with st.expander(f"Budget alerts ({len(alerts)})"):
        for a in alerts[:50]:
            st.markdown(f"`{ui.hhmm(a.get('ts'))}` {a.get('text')}")
else:
    st.caption(h.get("error"))
