"""Overview: today's headline numbers, bot health, budget, alerts and the latest AI view."""

import streamlit as st

from tradingbots.core import topics
from tradingbots.dashboard import data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
settings, _ = cli.settings()
st.title("Overview")

LEVEL_ICON = {"ERROR": "⛔", "WARNING": "⚠", "INFO": "ℹ"}


@st.fragment(run_every=settings.dashboard.refresh_s)
def live() -> None:
    t = T.tokens()
    usage = cli.kv(topics.KV_BUDGET_USAGE) or {}
    ex = cli.kv(topics.KV_EXEC_STATUS) or {}
    ms = {r["metric"]: r for r in cli.table("money_summary").to_dicts()}
    ai_hb = cli.heartbeats().get("ai_analyst", {}).get("details", {})
    k1, k2, k3, k4 = st.columns(4)
    nw = (ms.get("net_worth") or {}).get("value")
    k1.metric("Net worth", T.inr(nw, compact=True) if nw is not None else "-",
              help=(ms.get("net_worth") or {}).get("note") or "holdings + positions at market + cash")
    if ex.get("running"):
        net = ex.get("net_pnl")
        k2.metric("Today's P&L (trading engine)", f"{T.arrow(net)} {T.inr(net, signed=True)}",
                  help=f"{ex.get('mode')} mode · net of estimated charges")
    else:
        k2.metric("Today's P&L (trading engine)", "-", help=ex.get("note") or "the trading engine is not running")
    dc = next((i for i in usage.get("limits", []) if i["rule"] == "daily_capital"), None)
    if dc:
        k3.metric("Daily budget used", f"{dc['pct']:.0f}%", f"{T.inr(dc['remaining'])} left", delta_color="off",
                  delta_arrow="off")
    else:
        k3.metric("Daily budget used", "-", help="the budget bot has not reported yet")
    spent, cap = ai_hb.get("spent_today_usd"), ai_hb.get("budget_usd")
    k4.metric("AI spend today", T.usd(spent) if spent is not None else "-",
              f"of {T.usd(cap)} budget" if cap is not None else None, delta_color="off", delta_arrow="off")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Bots")
        ui.table(ui.bot_rows(cli))
        st.subheader("Latest alerts")
        alerts = cli.recent(topics.ALERT, 15)
        if alerts:
            for a in alerts:
                st.markdown(f"{LEVEL_ICON.get(a.get('level'), '•')} `{ui.hhmm(a.get('ts'))}` **{a.get('source')}** - "
                            f"{a.get('text')}")
        else:
            st.caption("No alerts.")
    with right:
        st.subheader("Daily budget")
        limits = usage.get("limits", [])
        if not limits:
            st.caption("No budget usage yet - the budget bot reports once it is running.")
        warn = (settings.budget.alert_at_pct or [80])[0]
        for it in limits[:5]:
            used = f"{it['used']:.0f} of {it['limit']:.0f}" if it["unit"] == "count" else \
                f"{T.inr(it['used'])} of {T.inr(it['limit'])}"
            st.markdown(T.meter_html(it["label"], used, it["pct"], t, warn_at=warn), unsafe_allow_html=True)
        st.subheader("AI view")
        rep = cli.recent(topics.AI_REPORT, 1)
        if rep and rep[0].get("status") == "ok":
            r = rep[0]
            st.markdown(f"**{r.get('headline')}**")
            st.caption(f"{str(r.get('kind', '')).replace('_', ' ')} · {ui.hhmm(r.get('ts'))} · {r.get('served_by')}")
        elif rep:
            st.caption(f"Last AI run did not complete: {rep[0].get('error') or rep[0].get('status')}")
        else:
            st.caption(ai_hb.get("reason") or "No AI report yet.")


live()
