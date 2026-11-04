"""AI insights: Claude's reports, a run-now button and what the analysis costs."""

import streamlit as st

from tradingbots.dashboard import charts, data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
settings, _ = cli.settings()
t = T.tokens()
st.title("AI insights")
st.caption("Decision support only. Nothing here places an order: the reports are read by you, never by the trading "
           "engine.")

st_ = cli.command("ai_analyst", "status")
if not st_.get("ok"):
    st.warning(st_.get("error"))
else:
    mode = st_.get("mode")
    if mode == "disabled":
        st.info(f"AI analysis is off: {st_.get('reason')}")
    elif mode == "stub":
        st.info("Demo mode: reports come from simple fixed rules, not from Claude (ai.provider = \"stub\").")
    k = st.columns(4)
    k[0].metric("Model", st_.get("model") if mode == "anthropic" else mode, f"effort {st_.get('effort')}",
                delta_color="off")
    k[1].metric("Spent today", T.usd(st_.get("spent_today_usd")), f"of {T.usd(st_.get('budget_usd'))}",
                delta_color="off")
    k[2].metric("Scheduled runs (IST)", ", ".join(st_.get("schedule") or []) or "none")
    k[3].metric("Refusal fallbacks", "on" if st_.get("refusal_fallbacks") else "off",
                help="when Claude declines a request, the API retries it on the model Anthropic recommends")
    cap = st_.get("budget_usd") or 0
    if cap:
        st.markdown(T.meter_html("Today's AI budget", f"{T.usd(st_.get('spent_today_usd'))} of {T.usd(cap)}",
                                 100 * (st_.get("spent_today_usd") or 0) / cap, t), unsafe_allow_html=True)
    if st_.get("running"):
        st.info(f"An analysis is running ({st_['running']}) - it usually takes under a minute.")
    c1, c2 = st.columns([1, 3])
    if c1.button("Run analysis now", type="primary", disabled=mode == "disabled" or bool(st_.get("running"))):
        ui.result(cli.command("ai_analyst", "run", {"kind": "on_demand"}),
                  "Started - the report appears here when it is ready.")

reps = (cli.command("ai_analyst", "reports", {"limit": 30}).get("reports") or [])
ok = [r for r in reps if r.get("status") == "ok"]
if not reps:
    st.caption("No reports yet.")
else:
    labels = {f"{ui.hhmm(r['ts'])} · {r['kind'].replace('_', ' ')} · {r.get('headline') or r.get('status')}": r["id"]
              for r in reps}
    pick = st.selectbox("Report", list(labels), index=0)
    full = (cli.command("ai_analyst", "report", {"id": labels[pick]}).get("report") or {})
    if full.get("status") != "ok":
        st.error(f"This run did not produce a report: {full.get('error') or full.get('status')}")
    else:
        st.markdown(full.get("markdown") or "")
    with st.expander("The data the analysis was given (packet)"):
        st.json(full.get("packet") or {}, expanded=False)

st.subheader("What the AI costs")
costs = cli.query("SELECT date AS day, SUM(usd) AS usd, SUM(input_tokens) AS input_tokens, "
                  "SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens, COUNT(*) AS calls "
                  "FROM ai_costs GROUP BY date ORDER BY date")
if costs.is_empty():
    st.caption("No AI calls yet.")
else:
    ui.chart(charts.columns([str(d) for d in costs["day"].to_list()], costs["usd"].to_list(), t, money=False,
                            hover_fmt="%{x}: $%{y:.4f}<extra></extra>"))
    st.caption(f"US dollars per day. The money bot books it as an expense at ₹{settings.money.usd_inr:g}/$.")
    with st.expander("Table view"):
        ui.table(costs)
