"""Tables: browse any table the bots keep, with its column descriptions, and download it as CSV."""

import streamlit as st

from tradingbots.core.schemas import TABLES
from tradingbots.dashboard import data, ui

cli = data.client()
st.title("Tables")

have = set(cli.available())
names = [n for n in TABLES if n in have] + sorted(have - set(TABLES))
if not names:
    st.info("No tables yet - they appear as the bots collect data.")
    st.stop()
c1, c2, c3 = st.columns([3, 2, 1])
name = c1.selectbox("Table", names, format_func=lambda n: f"{n} - {TABLES[n].description}" if n in TABLES else n)
td = TABLES.get(name)
where, params = "", []
if td is not None and td.partition == "date":
    days = cli.query(f"SELECT DISTINCT date FROM {name} ORDER BY date DESC LIMIT 400")
    opts = ["all days"] + [str(d) for d in (days["date"].to_list() if not days.is_empty() else [])]
    day = c2.selectbox("Day", opts)
    if day != "all days":
        where, params = "date = ?", [day]
elif td is not None and td.partition == "symbol":
    syms = cli.query(f"SELECT DISTINCT symbol FROM {name} ORDER BY symbol")
    opts = ["all"] + (syms["symbol"].to_list() if not syms.is_empty() else [])
    sym = c2.selectbox("Symbol", opts)
    if sym != "all":
        where, params = "symbol = ?", [sym]
limit = c3.number_input("Rows", 10, 100_000, 500, step=100)
df = cli.table(name, where, params, limit=int(limit))
st.caption(f"{df.height:,} rows shown · owned by the {td.owner if td else '?'} bot")
ui.table(df, height=480)
if not df.is_empty():
    st.download_button("Download CSV", df.write_csv(), file_name=f"{name}.csv", mime="text/csv")
if td is not None:
    with st.expander("Columns"):
        ui.table([{"Column": c, "Type": str(dt), "Meaning": desc} for c, (dt, desc) in td.columns.items()])
