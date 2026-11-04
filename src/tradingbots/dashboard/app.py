"""Trading bots dashboard. Run with ``bots run dashboard``, which calls
``streamlit run app.py -- --base <project folder>``.

Signs in with the password set by ``bots set-password`` and binds to 127.0.0.1 by default. The
dashboard only reads and sends commands: each change goes to the bot that owns it.
"""

from __future__ import annotations

import time

import streamlit as st

from tradingbots.core import topics
from tradingbots.core.secrets import load_env
from tradingbots.dashboard import auth, data, ui

st.set_page_config(page_title="Trading bots", page_icon=":material/monitoring:", layout="wide")

cli = data.client()
settings, version = cli.settings()
env = load_env(cli.base)

if not auth.gate(env.get("DASHBOARD_PASSWORD_HASH"), settings.dashboard.session_minutes):
    st.stop()

pages = [
    st.Page("pages/overview.py", title="Overview", icon=":material/dashboard:", default=True),
    st.Page("pages/market.py", title="Live market", icon=":material/show_chart:"),
    st.Page("pages/history.py", title="Stock history", icon=":material/candlestick_chart:"),
    st.Page("pages/money.py", title="Money", icon=":material/account_balance_wallet:"),
    st.Page("pages/budget.py", title="Daily budget", icon=":material/speed:"),
    st.Page("pages/trading.py", title="Trading", icon=":material/swap_horiz:"),
    st.Page("pages/ai.py", title="AI insights", icon=":material/psychology:"),
    st.Page("pages/tables.py", title="Tables", icon=":material/table_view:"),
    st.Page("pages/settings.py", title="Settings", icon=":material/settings:"),
]
nav = st.navigation(pages)

with st.sidebar:
    if cli.error:
        st.error(cli.error)
    ex = cli.kv(topics.KV_EXEC_STATUS) or {}
    rows = ui.bot_rows(cli)
    up = sum(r["State"] in ("running", "ok") for r in rows)
    st.caption(f"Platform **{settings.platform.name}** · settings v{version}")
    st.caption(f"Bots running **{up}/{len(rows)}**" if rows else "No bots running")
    mode = ex.get("mode")
    if ex.get("running") and mode == "live":
        st.error("Trading engine: **LIVE** - real orders")
    elif ex.get("running"):
        st.caption(f"Trading engine: {mode} · {ex.get('state', '')}")
    if st.button("Sign out", icon=":material/logout:"):
        st.session_state.pop("auth_at", None)
        st.rerun()
    st.caption(time.strftime("Updated %H:%M:%S"))

nav.run()
