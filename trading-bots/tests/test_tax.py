"""Tax estimate (India): the 12-month boundary, same-day netting, intraday and F&O, STT treatment, grandfathering,
purchases never guessed, set-off and exemption with known answers, and the bot end to end."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from dhanbot.clock import epoch_at, ist_datetime

from tradingbots.bots.tax import analytics as A
from tradingbots.core import topics
from tradingbots.core.settings import TaxCfg
from tradingbots.core.tables_io import TableReader, TableStore

from .team import Team


def tf(i: str, side: int, qty: int, price: float, d: date, hhmm: str = "10:00", product: str = "CNC",
       segment: str = "NSE_EQ", symbol: str = "INFY", stt: float = 0.0, other: float = 0.0) -> A.TFill:
    return A.TFill("dhan", symbol, segment, product, i, epoch_at(d, hhmm), side, qty, price, stt, other)


def by_cat(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["category"], []).append(r)
    return out


def test_financial_year_and_the_12_month_boundary():
    assert A.fy_of(date(2026, 3, 31)) == "2025-26" and A.fy_of(date(2026, 4, 1)) == "2026-27"
    assert A.fy_range("2026-27") == (date(2026, 4, 1), date(2027, 3, 31))
    assert not A.is_long_term(date(2025, 11, 5), date(2026, 11, 5))  # exactly 12 months is not "more than"
    assert A.is_long_term(date(2025, 11, 5), date(2026, 11, 6))
    assert A.add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert A.long_term_from(date(2024, 2, 29)) == date(2025, 3, 1)
    rows, held = A.tax_lots([tf("b", 1, 10, 100, date(2025, 11, 5)), tf("s1", -1, 4, 150, date(2026, 11, 5)),
                             tf("s2", -1, 4, 150, date(2026, 11, 6))])
    assert [r["category"] for r in rows] == ["stcg", "ltcg"] and rows[0]["holding_days"] == 365
    assert held[("dhan", "INFY")][0].qty == 2


def test_same_day_delivery_is_netted_as_intraday_and_the_rest_is_fifo():
    d0, d1 = date(2025, 6, 2), date(2026, 6, 10)
    rows, held = A.tax_lots([tf("old", 1, 10, 100, d0),
                             tf("s", -1, 10, 150, d1, "10:00"),  # sold the old shares in the morning ...
                             tf("b", 1, 6, 140, d1, "14:00")])  # ... bought 6 back the same afternoon
    c = by_cat(rows)
    assert c["intraday"][0]["qty"] == 6 and c["intraday"][0]["gross"] == 6 * (150 - 140)
    assert "same day" in c["intraday"][0]["remark"]
    assert c["ltcg"][0]["qty"] == 4 and c["ltcg"][0]["open_date"] == d0  # only 4 old shares were delivered
    assert [(lt.qty, lt.day) for lt in held[("dhan", "INFY")]] == [(6, d0)]  # the 6 still held are the old ones


def test_intraday_product_shorts_and_fno():
    d = date(2026, 7, 1)
    rows, held = A.tax_lots([tf("1", -1, 5, 200, d, "09:20", product="INTRADAY"),
                             tf("2", 1, 5, 190, d, "10:00", product="INTRADAY"),
                             tf("3", -1, 50, 120, d, "09:30", product="MARGIN", segment="NSE_FNO", symbol="NIFTY CE"),
                             tf("4", 1, 50, 80, date(2026, 7, 3), "11:00", product="MARGIN", segment="NSE_FNO",
                                symbol="NIFTY CE"),
                             tf("5", 1, 1, 700, d, product="MARGIN", segment="MCX_COMM", symbol="CRUDE")])
    c = by_cat(rows)
    assert c["intraday"][0]["side"] == "short" and c["intraday"][0]["gross"] == 50.0
    fno = c["fno"][0]
    assert fno["side"] == "short" and fno["gross"] == 50 * 40 and fno["open_date"] == d
    assert "unmatched" not in c and not held  # the open crude position is not a sale and not a holding


def test_stt_is_deductible_for_business_income_but_not_for_capital_gains():
    d0, d1 = date(2026, 4, 6), date(2026, 5, 6)
    rows, _ = A.tax_lots([tf("b", 1, 10, 100, d0, stt=1.0, other=2.0), tf("s", -1, 10, 110, d1, stt=1.1, other=2.2),
                          tf("ib", 1, 10, 100, d1, product="INTRADAY", symbol="TCS", stt=0.0, other=1.0),
                          tf("is", -1, 10, 110, d1, "11:00", product="INTRADAY", symbol="TCS", stt=0.25, other=1.0)])
    c = by_cat(rows)
    cg, it = c["stcg"][0], c["intraday"][0]
    assert cg["gross"] == 100.0 and cg["stt"] == 2.1 and cg["deductible_charges"] == 4.2 and cg["net"] == 95.8
    assert it["deductible_charges"] == 2.25 and it["net"] == 97.75


def test_grandfathered_cost_and_purchases_never_guessed():
    manual = [{"platform": "dhan", "symbol": "HDFCBANK", "qty": 10, "buy_date": "2016-05-10", "price": 500.0,
               "fmv_2018": 900.0}]
    rows, held = A.tax_lots([tf("s1", -1, 5, 1200, date(2026, 6, 1), symbol="HDFCBANK"),
                             tf("s2", -1, 5, 700, date(2026, 6, 2), symbol="HDFCBANK"),
                             tf("s3", -1, 3, 4000, date(2026, 6, 3), symbol="TCS")], manual)
    c = by_cat(rows)
    hi, lo = c["ltcg"]
    assert hi["open_price"] == 900.0 and hi["gross"] == 1500.0 and hi["grandfathered"] and hi["lot_source"] == "manual"
    assert lo["open_price"] == 700.0 and lo["gross"] == 0.0  # capped at the sale price
    un = c["unmatched"][0]
    assert un["symbol"] == "TCS" and un["qty"] == 3 and un["net"] is None and "add the purchase" in un["remark"]
    opens = A.open_lots(held, {("dhan", "INFY"): 7}, date(2026, 6, 3))
    assert opens == [{"platform": "dhan", "symbol": "INFY", "qty": 7, "buy_date": None, "buy_price": None,
                      "long_term_from": None, "days_to_long_term": None, "lot_source": "",
                      "remark": "needs a purchase date and price: bought before the trade history"}]


def _rows(fy: str, **nets: float) -> list[dict]:
    return [{"fy": fy, "category": c, "net": n, "gross": n, "deductible_charges": 0.0, "stt": 0.0} for c, n in nets.items()]


def test_set_off_exemption_and_tax_have_the_known_answers():
    cfg = TaxCfg()
    s = {r["category"]: r for r in A.summarize(_rows("2026-27", fno=-30000.0, intraday=20000.0, stcg=50000.0,
                                                          ltcg=200000.0), cfg)}
    assert s["fno"]["set_off"] == 30000.0 and s["intraday"]["after_set_off"] == 0.0  # slab 30% first
    assert s["stcg"]["after_set_off"] == 40000.0 and s["stcg"]["est_tax"] == pytest.approx(40000 * 0.20 * 1.04)
    assert s["ltcg"]["exemption"] == 125000.0 and s["ltcg"]["taxable"] == 75000.0
    assert s["ltcg"]["est_tax"] == pytest.approx(75000 * 0.125 * 1.04)
    assert s["total"]["est_tax"] == pytest.approx(8320.0 + 9750.0)
    # a speculative loss never reduces F&O income; it is left over
    s = {r["category"]: r for r in A.summarize(_rows("2026-27", intraday=-10000.0, fno=5000.0), cfg)}
    assert s["fno"]["taxable"] == 5000.0 and s["intraday"]["carry_forward"] == 10000.0
    assert s["total"]["est_tax"] == pytest.approx(5000 * 0.30 * 1.04)
    # a short-term loss reduces long-term gains before the exemption; surcharge on gains is capped at 15%
    rich = TaxCfg(surcharge_pct=25.0)
    s = {r["category"]: r for r in A.summarize(_rows("2026-27", stcg=-20000.0, ltcg=150000.0), rich)}
    assert s["ltcg"]["after_set_off"] == 130000.0 and s["ltcg"]["taxable"] == 5000.0
    assert s["ltcg"]["est_tax"] == pytest.approx(5000 * 0.125 * 1.15 * 1.04)
    s = {r["category"]: r for r in A.summarize(_rows("2026-27", fno=10000.0), rich)}
    assert s["fno"]["est_tax"] == pytest.approx(10000 * 0.30 * 1.25 * 1.04)  # business income: full surcharge
    years = {r["fy"] for r in A.summarize(_rows("2025-26", stcg=1.0) + _rows("2026-27", stcg=1.0), cfg)}
    assert years == {"2025-26", "2026-27"}


async def test_tax_bot_fetches_history_once_and_takes_manual_purchases(tmp_path, monkeypatch):
    team = Team(tmp_path, ["tax"])
    today = ist_datetime(team.clock.time()).date()
    fy_start = A.fy_range(A.fy_of(today))[0]
    store = TableStore(team.paths.tables)
    base = dict(platform="dhan", segment="NSE_EQ", product="CNC", source="manual")
    buy_day = date(fy_start.year - 1, 6, 3)
    store.replace("trades", [{**base, "trade_id": "B1", "symbol": "INFY", "side": "BUY", "qty": 10, "price": 1400.0,
                              "stt": 14.0, "total_charges": 20.0, "trade_ts": epoch_at(buy_day, "10:00")}], buy_day)
    sell_day = fy_start if fy_start.weekday() < 5 else date(fy_start.year, 4, 3)
    store.replace("trades", [{**base, "trade_id": "S1", "symbol": "INFY", "side": "SELL", "qty": 10, "price": 1600.0,
                              "stt": 16.0, "total_charges": 22.0, "trade_ts": epoch_at(sell_day, "11:00")},
                             {**base, "trade_id": "S2", "symbol": "TCS", "side": "SELL", "qty": 2, "price": 4000.0,
                              "stt": 8.0, "total_charges": 10.0, "trade_ts": epoch_at(sell_day, "11:30")}], sell_day)
    store.replace("holdings", [{"platform": "dhan", "symbol": "SBIN", "qty": 25, "avg_cost": 600.0}], today)
    asked: list[dict] = []
    responder = team.bus_factory("fake-market-data")

    async def market_data() -> None:
        topic = topics.cmd("market_data", "fetch_trades")
        await responder.connect()
        await responder.ensure_group("fake-md", [topic])
        while True:
            for m in await responder.consume("fake-md", [topic], block_s=0.2):
                corr = m.data.pop("_corr", None)
                asked.append(m.data)
                await responder.send_reply(corr, {"ok": True, "fetched": 3, "windows": 7, "errors": []})
                await responder.ack("fake-md", [m])

    task = asyncio.create_task(market_data())
    try:
        async with team:
            c = team.client
            from .helpers import wait_for

            assert await wait_for(lambda: asked, 15)
            assert asked[0] == {"from": f"{fy_start.year - 1}-04-01", "to": today.isoformat()}
            st = await c.request(topics.cmd("tax", "status"), {})
            assert st["history"]["covered_from"] == f"{fy_start.year - 1}-04-01" and st["unmatched_sales"] == 1
            assert st["holdings_without_date"] == 1
            reader = TableReader(team.paths.tables)
            infy = reader.query("SELECT * FROM tax_lots WHERE symbol = 'INFY'").to_dicts()[0]
            assert infy["category"] == "ltcg" if (sell_day - buy_day).days > 366 else infy["category"] == "stcg"
            assert infy["gross"] == 2000.0 and infy["deductible_charges"] == 12.0 and infy["stt"] == 30.0

            bad = await c.request(topics.cmd("tax", "add_lot"), {"symbol": "TCS", "qty": 2, "buy_date": "2019-01-01",
                                                                 "price": 1900.0, "fmv_2018": 1000.0})
            assert not bad["added"] and "1 Feb 2018" in bad["error"]
            ok = await c.request(topics.cmd("tax", "add_lot"), {"symbol": "tcs", "qty": 2, "buy_date": "2019-01-01",
                                                                "price": 1900.0})
            assert ok["added"]
            st = await c.request(topics.cmd("tax", "status"), {})
            assert st["unmatched_sales"] == 0 and st["manual_lots"] == 1
            tcs = reader.query("SELECT * FROM tax_lots WHERE symbol = 'TCS'").to_dicts()[0]
            assert tcs["category"] == "ltcg" and tcs["lot_source"] == "manual" and tcs["gross"] == 4200.0
            summ = {r["category"]: r for r in reader.query("SELECT * FROM tax_summary WHERE fy = ?",
                                                           [A.fy_of(today)]).to_dicts()}
            assert summ["total"]["est_tax"] >= 0 and summ["ltcg"]["matches"] >= 1

            await team.stop("tax")  # the history is fetched once: a restart does not ask again
            await team.start("tax")
            await asyncio.sleep(1.0)
            assert len(asked) == 1
            again = await c.request(topics.cmd("tax", "backfill"), {})  # unless asked to
            assert again["done"] and len(asked) == 2

            from tradingbots.cli import cmd_tax

            monkeypatch.chdir(tmp_path)
            (tmp_path / "config").mkdir(exist_ok=True)
            from tradingbots.core.settings import write_settings_file

            write_settings_file(tmp_path / "config" / "settings.toml", team.settings)
            assert cmd_tax(tmp_path, A.fy_of(today), str(tmp_path / "sales.csv")) == 0
            assert "TCS" in (tmp_path / "sales.csv").read_text()
    finally:
        task.cancel()
        await responder.close()


def test_settings_reject_a_bad_history_date():
    from tradingbots.core.settings import SettingsError, parse_settings

    with pytest.raises(SettingsError):
        parse_settings({"tax": {"history_from": "1st April"}})
    assert SimpleNamespace(**TaxCfg().model_dump()).ltcg_exemption_inr == 125000.0
