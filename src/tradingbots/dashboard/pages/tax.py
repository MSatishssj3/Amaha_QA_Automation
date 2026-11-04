"""Tax (India): the estimated tax on your trades per financial year, what still needs your input, when the shares you
hold turn long-term, and statements for your CA. An estimate, not tax advice."""

from datetime import date

import polars as pl
import streamlit as st

from tradingbots.bots.tax import analytics as A
from tradingbots.dashboard import data, ui
from tradingbots.dashboard import theme as T

cli = data.client()
settings, _ = cli.settings()
tx = settings.tax
st.title("Tax (India)")
st.caption(f"An estimate to discuss with your CA, not tax advice. Financial year 1 April - 31 March. Rates in use: "
           f"short-term {tx.stcg_rate_pct:g}%, long-term {tx.ltcg_rate_pct:g}% above {T.inr(tx.ltcg_exemption_inr)}, "
           f"intraday and F&O at your {tx.slab_rate_pct:g}% slab, cess {tx.cess_pct:g}%"
           + (f", surcharge {tx.surcharge_pct:g}%" if tx.surcharge_pct else "") + ".")
if not tx.enabled:
    st.warning("The tax estimate is switched off (tax.enabled = false).")

summary = cli.table("tax_summary")
lots = cli.table("tax_lots")
today = ui.team_today(cli)
fys = sorted(set(summary["fy"].to_list()) | {A.fy_of(today)}, reverse=True) if not summary.is_empty() else [A.fy_of(today)]
fy = st.selectbox("Financial year", fys, index=0)
rows = {r["category"]: r for r in summary.filter(pl.col("fy") == fy).to_dicts()} if not summary.is_empty() else {}
tot = rows.get("total", {})


def day(d) -> str:
    return f"{d:%d %b %Y}" if d else "-"


def cat(c: str, k: str) -> float:
    return float((rows.get(c) or {}).get(k) or 0.0)


k = st.columns(4)
k[0].metric("Estimated tax", T.inr(tot.get("est_tax", 0.0)), f"on {T.inr(tot.get('taxable', 0.0))} taxable",
            delta_color="off", delta_arrow="off", help="with surcharge and cess; before any losses carried in from "
                                                        "earlier years and without your other income")
k[1].metric("Short-term gains", T.inr(cat("stcg", "after_set_off"), signed=True),
            f"tax {T.inr(cat('stcg', 'est_tax'))} at {tx.stcg_rate_pct:g}%", delta_color="off", delta_arrow="off")
k[2].metric("Long-term gains", T.inr(cat("ltcg", "after_set_off"), signed=True),
            f"{T.inr(cat('ltcg', 'exemption'))} exempt · tax {T.inr(cat('ltcg', 'est_tax'))}", delta_color="off",
            delta_arrow="off")
biz = cat("intraday", "after_set_off") + cat("fno", "after_set_off")
k[3].metric("Intraday + F&O income", T.inr(biz, signed=True),
            f"tax {T.inr(cat('intraday', 'est_tax') + cat('fno', 'est_tax'))} at your {tx.slab_rate_pct:g}% slab",
            delta_color="off", delta_arrow="off")

opens = cli.table("tax_open_lots")
missing = opens.filter(pl.col("buy_date").is_null()) if not opens.is_empty() else opens
unmatched = lots.filter(pl.col("category") == "unmatched") if not lots.is_empty() else lots
needs = [x for x in (f"{T.plural(unmatched.height, 'sale')} without a purchase" if unmatched.height else "",
                     f"{T.plural(missing.height, 'holding')} without a purchase date" if missing.height else "") if x]
if needs:
    st.warning(f"Needs your input: {' and '.join(needs)}. Add the purchases under **Needs your input** below; "
               "until then they are left out of the estimate.")

st.subheader("By category")
order = ["intraday", "fno", "stcg", "ltcg", "total"]
ui.table([{"Category": r["label"], "Sales": r["matches"], "Net": T.inr(r["net"], signed=True),
           "Set-off": T.inr(r["set_off"], signed=True) if r["set_off"] else "-",
           "After set-off": T.inr(r["after_set_off"], signed=True), "Exempt": T.inr(r["exemption"]) if r["exemption"]
           else "-", "Taxable": T.inr(r["taxable"]), "Rate": f"{r['rate_pct']:g}%" if r["rate_pct"] is not None else "",
           "Est. tax": T.inr(r["est_tax"]), "Loss left over": T.inr(r["carry_forward"]) if r["carry_forward"]
           else "-"} for c in order if (r := rows.get(c))])
st.caption("Net = gains + losses after deductible charges. Gains and losses per category are in the CSV summary.")
if rows:
    st.caption(f"Set-off: a short-term loss also reduces long-term gains; an F&O loss reduces intraday, short- and "
               f"long-term gains (highest rate first); an intraday loss only intraday gains. A loss left over can be "
               f"carried forward if you file on time - not tracked here. Turnover for the tax-audit check (intraday + "
               f"F&O): {T.inr(cat('intraday', 'turnover') + cat('fno', 'turnover'))}. STT paid on capital-gains trades: "
               f"{T.inr(cat('stcg', 'stt') + cat('ltcg', 'stt'))} (not deductible).")

fy_lots = lots.filter(pl.col("fy") == fy).sort("close_date") if not lots.is_empty() else lots
c1, c2 = st.columns(2)
c1.download_button("Download this year's sales (CSV)", fy_lots.write_csv() if fy_lots.height else "",
                   file_name=f"tax-sales-{fy}.csv", mime="text/csv", disabled=not fy_lots.height,
                   help="every matched sale with purchase and sale dates, prices, charges and category - for your CA")
fy_sum = summary.filter(pl.col("fy") == fy) if not summary.is_empty() else summary
c2.download_button("Download the summary (CSV)", fy_sum.write_csv() if fy_sum.height else "",
                   file_name=f"tax-summary-{fy}.csv", mime="text/csv", disabled=not fy_sum.height)
with st.expander(f"Every sale in {fy} ({fy_lots.height})"):
    ui.table([{"Sold": day(r["close_date"]), "Stock": r["symbol"], "Category": r["category"], "Qty": r["qty"],
               "Bought": day(r["open_date"]), "Cost / unit": r["open_price"], "Sale / unit": r["close_price"],
               "Held (days)": r["holding_days"], "Charges": r["deductible_charges"], "STT": r["stt"], "Net": r["net"],
               "Note": r["remark"]} for r in fy_lots.to_dicts()])

st.subheader("Needs your input")
st.caption("Shares bought before the trade history have no purchase date. Add each purchase here (quantity, date and "
           "price per share); it is matched first in, first out. Nothing is guessed.")
if unmatched.height:
    st.markdown("**Sales without a purchase**")
    ui.table([{"Sold": day(r["close_date"]), "Stock": r["symbol"], "Qty": r["qty"], "Price": r["close_price"],
               "Platform": r["platform"]} for r in unmatched.to_dicts()])
if missing.height:
    st.markdown("**Holdings without a purchase date**")
    ui.table([{"Stock": r["symbol"], "Qty": r["qty"], "Platform": r["platform"]} for r in missing.to_dicts()])
with st.form("add_lot", clear_on_submit=True):
    a, b, c, d = st.columns(4)
    sym = a.text_input("Stock symbol", placeholder="e.g. INFY")
    qty = b.number_input("Quantity", min_value=0, step=1)
    bought = c.date_input("Bought on", value=None, min_value=date(1990, 1, 1), max_value=today)
    price = d.number_input("Price per share (₹)", min_value=0.0, step=1.0)
    fmv = a.number_input("31 Jan 2018 price (only if bought before 1 Feb 2018)", min_value=0.0, step=1.0)
    note = b.text_input("Note")
    if st.form_submit_button("Add purchase"):
        if not (sym.strip() and qty and bought and price):
            st.error("Stock, quantity, date and price are all needed.")
        else:
            ui.result(cli.command("tax", "add_lot", {"symbol": sym, "qty": int(qty), "buy_date": bought.isoformat(),
                                                     "price": price, "fmv_2018": fmv or None, "note": note}),
                      "Added - the estimate updates in a few seconds.")
manual = cli.command("tax", "lots").get("lots") or []
if manual:
    with st.expander(f"Purchases you added ({len(manual)})"):
        ui.table([{"Stock": m["symbol"], "Qty": m["qty"], "Bought": m["buy_date"], "Price": T.inr(m["price"]),
                   "31 Jan 2018 price": T.inr(m["fmv_2018"]) if m["fmv_2018"] else "-", "Note": m["note"] or ""}
                  for m in manual])
        opts = {f"{m['symbol']} · {m['qty']} on {m['buy_date']} at {T.inr(m['price'])}": m["id"] for m in manual}
        sel = st.selectbox("Remove", list(opts))
        if st.button("Remove this purchase"):
            ui.result(cli.command("tax", "delete_lot", {"id": opts[sel]}), "Removed.")

st.subheader("Shares you hold: when each lot turns long-term")
held = opens.filter(pl.col("buy_date").is_not_null()).sort("long_term_from") if not opens.is_empty() else opens
if held.height:
    soon = held.filter((pl.col("days_to_long_term") > 0) & (pl.col("days_to_long_term") <= 30))
    if soon.height:
        st.info("Within 30 days of turning long-term: " + ", ".join(
            f"{r['symbol']} ({r['qty']} in {T.plural(r['days_to_long_term'], 'day')})" for r in soon.to_dicts())
            + f". Sold before then the gain is short-term ({tx.stcg_rate_pct:g}%), after it long-term "
              f"({tx.ltcg_rate_pct:g}% above the yearly exemption).")
    ui.table([{"Stock": r["symbol"], "Qty": r["qty"], "Bought": day(r["buy_date"]), "Price": r["buy_price"],
               "Long-term from": day(r["long_term_from"]),
               "Days to go": str(r["days_to_long_term"]) if r["days_to_long_term"] else "long-term now",
               "From": r["lot_source"]} for r in held.to_dicts()])
else:
    st.caption("No delivery holdings with a known purchase date.")

with st.expander("Tax settings and trade history"):
    hist = (cli.command("tax", "status").get("history") or {}) if ui.bot_running(cli, "tax") else {}
    if hist.get("covered_from"):
        st.caption(f"Trade history fetched from {hist['covered_from']} ({hist.get('fetched_trades', 0):,} trades).")
    elif hist.get("last_error"):
        st.caption(f"Trade history not fetched yet: {hist['last_error']}")
    if st.button("Fetch the trade history again", disabled=not ui.bot_running(cli, "tax")):
        ui.result(cli.command("tax", "backfill", {}, timeout=900), "Fetched.")
    with st.form("tax_settings"):
        a, b, c = st.columns(3)
        slab = a.number_input("Your slab rate for intraday / F&O income (%)", 0.0, 50.0, tx.slab_rate_pct, step=5.0)
        sur = b.number_input("Your surcharge (%)", 0.0, 37.0, tx.surcharge_pct, step=5.0,
                             help="by income level; on capital gains it is capped at 15%")
        since = c.text_input("Fetch trade history from (YYYY-MM-DD; empty = previous FY)", tx.history_from)
        stcg = a.number_input("Short-term rate (%)", 0.0, 50.0, tx.stcg_rate_pct, step=0.5)
        ltcg = b.number_input("Long-term rate (%)", 0.0, 50.0, tx.ltcg_rate_pct, step=0.5)
        ex = c.number_input("Long-term exemption per year (₹)", 0.0, value=tx.ltcg_exemption_inr, step=5000.0)
        if st.form_submit_button("Save tax settings", type="primary"):
            ui.result(cli.command("config", "update", {"patch": {"tax": {
                "slab_rate_pct": slab, "surcharge_pct": sur, "history_from": since.strip(), "stcg_rate_pct": stcg,
                "ltcg_rate_pct": ltcg, "ltcg_exemption_inr": ex}}, "author": "dashboard", "note": "tax"}), "Saved.")
