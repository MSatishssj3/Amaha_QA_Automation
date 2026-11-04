"""Settings: the trading platform, watchlist, schedules, AI options and Dhan token, with version history.

Every change goes to the config bot. It validates the change (listing every problem at once), saves it
as a new version and tells the other bots, so most changes apply without a restart.
"""

import time

import streamlit as st

from dhanbot.dhan.endpoints import SEGMENT_TO_CODE
from tradingbots.dashboard import data, ui

cli = data.client()
settings, version = cli.settings()
st.title("Settings")
st.caption(f"Settings version {version}. Saved changes are versioned; any version can be restored below.")


def save(patch: dict, note: str) -> None:
    ui.result(cli.command("config", "update", {"patch": patch, "author": "dashboard", "note": note}), "Saved.")


tab_p, tab_w, tab_s, tab_a, tab_t, tab_h = st.tabs(["Platform", "Watchlist", "Schedules", "AI", "Dhan token",
                                                    "History"])

with tab_p:
    st.write("Where market data comes from (and where the trading engine trades).")
    choice = st.radio("Trading platform", ["simulated", "dhan"], index=["simulated", "dhan"].index(settings.platform.name),
                      captions=["built-in simulated market - no account, no real money", "your Dhan account"],
                      horizontal=True)
    auth = st.radio("Dhan login", ["token", "totp"], index=["token", "totp"].index(settings.platform.dhan_auth),
                    captions=["paste a daily access token (Dhan token tab)",
                              "automatic daily login with DHAN_PIN and DHAN_TOTP_SECRET in .env"], horizontal=True)
    if choice != settings.platform.name:
        st.warning("Switching the platform reconnects market data now. The trading engine switches only when it has "
                   "no open positions or orders, otherwise at the next session.")
    if st.button("Save platform", type="primary"):
        save({"platform": {"name": choice, "dhan_auth": auth}}, "platform")

with tab_w:
    st.write("Stocks to stream and analyse. Indices (segment IDX_I) need their security id, e.g. NIFTY = 13.")
    rows = [w.model_dump() for w in settings.watchlist]
    edited = st.data_editor(rows, num_rows="dynamic", width="stretch", key="wl", column_config={
        "symbol": st.column_config.TextColumn("Symbol", required=True),
        "segment": st.column_config.SelectboxColumn("Segment", options=sorted(SEGMENT_TO_CODE), required=True),
        "security_id": st.column_config.NumberColumn("Security id", min_value=0, step=1),
        "mode": st.column_config.SelectboxColumn("Feed", options=["ticker", "quote", "full"], required=True)})
    if st.button("Save watchlist", type="primary"):
        clean = [{k: v for k, v in r.items() if v is not None and v != ""} for r in edited if r.get("symbol")]
        for r in clean:
            r["symbol"] = str(r["symbol"]).strip().upper()
            r["security_id"] = int(r.get("security_id") or 0)
        save({"watchlist": clean}, "watchlist")

with tab_s:
    sc = settings.schedule
    with st.form("sched"):
        a, b = st.columns(2)
        runs = a.text_input("AI analysis times (IST, comma separated)", ", ".join(sc.ai_runs))
        hist = b.text_input("Daily history refresh (IST)", sc.history_refresh)
        summ = a.text_input("Budget end-of-day summary (IST)", sc.budget_summary)
        money = b.number_input("Money recompute every (seconds)", 30.0, value=sc.money_interval_s, step=30.0)
        snap = a.number_input("Account snapshot every (seconds)", 10.0, value=settings.market_data.snapshot_interval_s,
                              step=10.0)
        if st.form_submit_button("Save schedules", type="primary"):
            save({"schedule": {"ai_runs": [x.strip() for x in runs.split(",") if x.strip()], "history_refresh": hist,
                               "budget_summary": summ, "money_interval_s": money},
                  "market_data": {"snapshot_interval_s": snap}}, "schedules")

with tab_a:
    ai = settings.ai
    with st.form("ai"):
        a, b = st.columns(2)
        enabled = a.toggle("AI analysis on", value=ai.enabled)
        provider = b.selectbox("Provider", ["anthropic", "stub"], index=["anthropic", "stub"].index(ai.provider),
                               help="stub = rule-based demo reports, no AI and no cost")
        model = a.text_input("Model", ai.model)
        effort = b.selectbox("Effort", ["low", "medium", "high", "xhigh", "max"],
                             index=["low", "medium", "high", "xhigh", "max"].index(ai.effort),
                             help="higher = deeper analysis, more tokens")
        budget = a.number_input("Daily AI budget (US$)", 0.0, value=ai.daily_usd_budget, step=0.25)
        stocks = b.number_input("Max stocks per report", 1, 200, ai.max_stocks)
        fallbacks = a.toggle("Refusal fallbacks", value=ai.refusal_fallbacks,
                             help="if Claude declines a request, the API retries it on Anthropic's recommended model")
        nightly = b.toggle("Nightly deep dives (Batches API, half price)", value=ai.nightly_batch)
        if st.form_submit_button("Save AI settings", type="primary"):
            save({"ai": {"enabled": enabled, "provider": provider, "model": model.strip(), "effort": effort,
                         "daily_usd_budget": budget, "max_stocks": int(stocks), "refusal_fallbacks": fallbacks,
                         "nightly_batch": nightly}}, "ai")
    if st.button("Test the API key", help="one tiny call - a fraction of a cent"):
        r = cli.command("ai_analyst", "test", {}, timeout=60)
        if r.get("ok_key"):
            st.success(f"The key works: {r.get('served_by')} replied in {r.get('seconds')}s "
                       f"(cost ${r.get('cost_usd') or 0:.5f}).")
        else:
            st.error(r.get("error") or r.get("reason") or "The test failed.")

with tab_t:
    st.write("The config bot is the only bot that logs in to Dhan; it shares the token with the others.")
    ts = cli.command("config", "token_status")
    if ts.get("ok"):
        info = ts.get("info") or {}
        left = ts.get("seconds_left")
        shared = bool(info.get("updated") or info.get("version")) and not ts.get("error")
        st.metric(f"Token ({ts.get('platform')})", "shared with the bots" if shared else "missing",
                  f"{left / 3600:.1f} h left" if left else None, delta_color="off")
        if ts.get("error"):
            st.error(ts["error"])
    else:
        st.caption(ts.get("error"))
    with st.form("token", clear_on_submit=True):
        tok = st.text_input("Paste a new Dhan access token", type="password",
                            help="web.dhan.co -> My profile -> Access DhanHQ APIs")
        if st.form_submit_button("Save token") and tok.strip():
            ui.result(cli.command("config", "set_token", {"token": tok.strip()}), "Token saved and shared.")

with tab_h:
    h = cli.command("config", "history", {"limit": 50})
    vers = h.get("versions") or []
    ui.table([{"Version": v["version"], "When": time.strftime("%d %b %H:%M", time.localtime(v["ts"])),
               "By": v["author"], "Note": v.get("note") or "", "Changes": "; ".join(v.get("changes") or [])[:300]}
              for v in vers])
    if vers:
        with st.popover("Restore a version"):
            target = st.selectbox("Version", [v["version"] for v in vers])
            if st.button("Restore", type="primary"):
                ui.result(cli.command("config", "rollback", {"version": int(target)}), f"Restored version {target}.")
