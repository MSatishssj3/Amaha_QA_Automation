"""Trading: the dhan_bot engine's state, today's orders and fills, and its controls."""

import streamlit as st

from tradingbots.core import topics
from tradingbots.dashboard import data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
settings, _ = cli.settings()
st.title("Trading")


@st.fragment(run_every=settings.dashboard.refresh_s)
def live() -> None:
    ex = cli.kv(topics.KV_EXEC_STATUS) or {}
    if not ex:
        st.info("The execution bot is not running.")
        return
    if not ex.get("running"):
        st.info(f"Trading engine not running: {ex.get('note') or 'idle'}")
    mode = ex.get("mode") or "-"
    if ex.get("running") and mode == "live":
        st.error("**LIVE mode** - this engine places real orders with real money.")
    k = st.columns(5)
    k[0].metric("Mode", mode, ex.get("platform"), delta_color="off")
    k[1].metric("State", ex.get("state", "-"), ex.get("reason") or None, delta_color="off")
    net = ex.get("net_pnl")
    k[2].metric("Net P&L today", f"{T.arrow(net)} {T.inr(net, signed=True)}" if net is not None else "-",
                f"charges {T.inr(ex.get('charges'))}" if ex.get("charges") is not None else None, delta_color="off")
    k[3].metric("Open positions", ex.get("open_positions", 0), f"{ex.get('working_orders', 0)} working orders",
                delta_color="off")
    g = ex.get("guard") or {}
    k[4].metric("Budget used by the engine", T.inr(g.get("turnover_inr")),
                f"{T.inr(g.get('reserved_inr'))} reserved by open entries" if g else None, delta_color="off")
    if g.get("policy_problem"):
        st.warning(f"New entries are blocked: {g['policy_problem']}")
    pnl = (cli.recent(topics.EXEC_PNL, 1) or [{}])[0]
    if pnl.get("positions"):
        st.subheader("Open positions")
        ui.table(pnl["positions"])
    rej = [r for r in cli.recent(topics.EXEC_REJECT, 30)]
    if rej:
        with st.expander(f"Entries refused before sending ({len(rej)})"):
            ui.table([{"Time": ui.hhmm(r.get("ts")), "Stock": r.get("symbol"), "Rule": r.get("rule"),
                       "Times": r.get("count"), "Reason": r.get("reason")} for r in rej])


live()

st.subheader("Controls")
running = bool((cli.kv(topics.KV_EXEC_STATUS) or {}).get("running"))
c1, c2, c3, c4 = st.columns(4)
if c1.button("Pause new entries", disabled=not running, help="exits and protective orders keep working"):
    ui.result(cli.command("execution", "control", {"action": "pause"}), "Paused.")
if c2.button("Resume", disabled=not running):
    ui.result(cli.command("execution", "control", {"action": "resume"}), "Resumed.")
with c3.popover("Flatten all", disabled=not running):
    st.write("Squares off every position the engine opened and pauses new entries.")
    if st.checkbox("Yes, flatten now", key="confirm_flatten") and st.button("Flatten", type="primary"):
        ui.result(cli.command("execution", "control", {"action": "flatten"}, timeout=45), "Flatten sent.")
with c4.popover("Kill for today", disabled=not running):
    st.write("Halts trading for the rest of the day: cancels the engine's orders and flattens its positions. "
             "It cannot be resumed today.")
    if st.checkbox("Yes, halt for today", key="confirm_kill") and st.button("Halt", type="primary"):
        ui.result(cli.command("execution", "control", {"action": "kill"}), "Halted for today.")

on = settings.execution.enabled
new = st.toggle("Run my dhan_bot strategy here (execution.enabled)", value=on,
                help=f"uses {settings.execution.dhanbot_config}; real orders also need execution.live and "
                     "mode = \"live\" in that file")
if new != on:
    ui.result(cli.command("config", "update", {"patch": {"execution": {"enabled": new}}, "author": "dashboard"}),
              "Saved.")

today = ui.team_today(cli)
st.subheader("Today's orders")
ui.table(cli.table("exec_orders", "date = ?", [today], order="updated_ts DESC"))
st.subheader("Today's fills")
ui.table(cli.table("exec_fills", "date = ?", [today], order="ts DESC"))
