"""Performance journal: round trips (scaling, reversals, shorts, earlier shares, paper), statistics with known
answers, breakdowns, and the bot end to end (tables, slippage, exit reasons, tags and notes that survive)."""

from __future__ import annotations

from datetime import date

import pytest
from dhanbot.clock import epoch_at, ist_datetime

from tradingbots.bots.journal import analytics as A
from tradingbots.core import topics
from tradingbots.core.tables_io import TableReader, TableStore

from .team import Team

D = date(2026, 11, 5)  # a Thursday


def ts(hhmm: str, d: date = D) -> float:
    return epoch_at(d, hhmm)


def fill(i: str, side: int, qty: int, price: float, at: str, charges: float = 0.0, symbol: str = "RELIANCE",
         product: str = "INTRADAY", book: str = "real", source: str = "manual", **kw) -> A.Fill:
    return A.Fill(book, "dhan", symbol, product, i, ts(at), side, qty, price, charges, source, "NSE_EQ", **kw)


def test_scaling_in_and_out_is_one_round_trip():
    trips = A.round_trips([fill("1", 1, 10, 100, "09:20", 5), fill("2", 1, 10, 110, "09:30", 5),
                           fill("3", -1, 15, 120, "10:00", 6), fill("4", -1, 5, 90, "10:30", 2)])
    assert len(trips) == 1
    t = trips[0]
    assert t["status"] == "closed" and t["direction"] == "long" and t["qty"] == 20 and t["max_qty"] == 20
    assert t["avg_entry"] == 105 and t["avg_exit"] == 112.5 and t["fills"] == 4
    assert t["gross"] == 150.0 and t["charges"] == 18.0 and t["net"] == 132.0
    assert t["holding_s"] == 70 * 60 and t["exit_day"] == D and t["entry_day"] == D
    assert t["return_pct"] == pytest.approx(132 / 2100 * 100, abs=1e-3)
    assert t["trade_key"] == "real:dhan:RELIANCE:INTRADAY:1"


def test_a_reversal_closes_one_trade_and_opens_the_next_splitting_the_charges():
    trips = A.round_trips([fill("1", 1, 10, 100, "09:20", 10), fill("2", -1, 15, 105, "09:40", 15),
                           fill("3", 1, 5, 100, "10:00", 2)])
    long_, short = trips
    assert long_["direction"] == "long" and long_["gross"] == 50.0 and long_["charges"] == 20.0 and long_["net"] == 30.0
    assert short["direction"] == "short" and short["qty"] == 5 and short["gross"] == 25.0
    assert short["charges"] == 7.0 and short["net"] == 18.0 and short["trade_key"].endswith(":2")
    one_open = A.round_trips([fill("1", -1, 10, 200, "09:20"), fill("2", 1, 4, 190, "09:50")])
    assert one_open[0]["status"] == "open" and one_open[0]["open_qty"] == 6 and one_open[0]["net"] is None


def test_earlier_shares_use_the_holding_cost_or_stay_out_of_the_stats():
    trips = A.round_trips([fill("s1", -1, 5, 300, "10:00", 5, symbol="INFY", product="CNC"),
                           fill("s2", -1, 2, 4000, "10:05", 4, symbol="TCS", product="CNC")],
                          holding_cost={("dhan", "INFY"): 250.0})
    infy, tcs = trips
    assert infy["status"] == "closed" and infy["avg_entry"] == 250.0 and infy["gross"] == 250.0 and infy["net"] == 245.0
    assert "average price" in infy["remark"] and infy["entry_ts"] is None
    assert tcs["status"] == "unmatched" and tcs["net"] is None and "left out" in tcs["remark"]
    assert A.stats(trips)["trades"] == 1
    # a paper sale is a short, never "earlier shares"; paper and real books never merge
    mixed = A.round_trips([fill("r1", 1, 10, 100, "09:20"), fill("p1", -1, 10, 101, "09:21", book="paper",
                                                                 source="paper", product="CNC")])
    assert {(t["book"], t["status"], t["direction"]) for t in mixed} == {("real", "open", "long"),
                                                                        ("paper", "open", "short")}


def test_slippage_and_how_the_trade_ended():
    trips = A.round_trips([fill("1", 1, 10, 100.5, "09:20", source="bot", role="entry", ref_price=100.0),
                           fill("2", -1, 10, 109.8, "11:00", source="bot", role="sl", ref_price=110.0)])
    t = trips[0]
    assert t["slippage"] == pytest.approx(5.0 + 2.0) and t["exit_reason"] == "stop-loss" and t["source"] == "bot"
    manual_exit = A.round_trips([fill("1", 1, 1, 100, "09:20", source="bot", role="entry"),
                                 fill("2", -1, 1, 101, "09:30", source="manual")])[0]
    assert manual_exit["source"] == "mixed" and manual_exit["exit_reason"] == "manual" and manual_exit["slippage"] is None


def _closed(nets: list[float]) -> list[dict]:
    return [{"trade_key": f"k{i}", "status": "closed", "net": x, "gross": x + 10, "charges": 10.0,
             "exit_ts": 1000.0 + i, "holding_s": 60.0 * (i + 1), "slippage": None} for i, x in enumerate(nets)]


def test_statistics_have_the_known_answers():
    s = A.stats(_closed([100, -50, -30, 200, -40]))
    assert (s["trades"], s["wins"], s["losses"]) == (5, 2, 3)
    assert s["win_rate"] == 40.0 and s["net_pnl"] == 180.0 and s["expectancy"] == 36.0
    assert s["avg_win"] == 150.0 and s["avg_loss"] == -40.0 and s["payoff"] == 3.75
    assert s["profit_factor"] == 2.5 and s["max_drawdown"] == 80.0  # 100 -> 20, then 220 -> 180
    assert s["losing_streak"] == 2 and s["winning_streak"] == 1 and s["best"] == 200.0 and s["worst"] == -50.0
    assert s["gross_pnl"] == 230.0 and s["charges"] == 50.0 and s["charges_pct_of_gross"] == pytest.approx(21.74)
    assert s["avg_hold_s"] == 180.0
    assert A.stats(_closed([10, 20]))["profit_factor"] is None  # no losing trade: not a number
    empty = A.stats([])
    assert empty["trades"] == 0 and empty["win_rate"] is None and empty["net_pnl"] == 0.0
    assert A.equity_curve(_closed([5, -2]))[-1][1] == 3.0


def test_breakdowns_scopes_and_tags():
    trips = A.round_trips([fill("1", 1, 1, 100, "09:20"), fill("2", -1, 1, 110, "09:50"),
                           fill("3", 1, 1, 100, "14:05", symbol="INFY"), fill("4", -1, 1, 95, "15:00", symbol="INFY")])
    trips[0]["tags"] = "breakout,news"
    hours = A.breakdown(trips, "hour")
    assert [(r["bucket"], r["net_pnl"]) for r in hours] == [("09:00", 10.0), ("14:00", -5.0)]
    assert [r["bucket"] for r in A.breakdown(trips, "tag")] == ["breakout", "news", "untagged"]
    assert A.breakdown(trips, "weekday")[0]["bucket"] == "Thu"
    assert [r["bucket"] for r in A.breakdown(trips, "holding")] == ["30 min - 2 h"]
    assert A.in_scope(trips[0], "fy", date(2027, 3, 31)) and not A.in_scope(trips[0], "fy", date(2027, 4, 1))
    assert A.fy_start(date(2026, 3, 31)) == date(2025, 4, 1) and A.fy_start(date(2026, 4, 1)) == date(2026, 4, 1)
    assert A.parse_tags(" Breakout,  NEWS ,breakout,, revenge trade") == ["breakout", "news", "revenge trade"]


async def test_journal_bot_end_to_end(tmp_path):
    team = Team(tmp_path, ["journal"])
    day = ist_datetime(team.clock.time()).date()

    def at(hhmm: str) -> float:
        return epoch_at(day, hhmm)

    store = TableStore(team.paths.tables)
    trade = dict(platform="dhan", segment="NSE_EQ", product="INTRADAY", source="manual")
    store.replace("trades", [
        {**trade, "trade_id": "T1", "order_id": "O1", "symbol": "RELIANCE", "side": "BUY", "qty": 10, "price": 1450.0,
         "total_charges": 6.0, "trade_ts": at("09:21"), "source": "bot"},
        {**trade, "trade_id": "T2", "order_id": "O2", "symbol": "RELIANCE", "side": "SELL", "qty": 10, "price": 1460.0,
         "total_charges": 8.0, "trade_ts": at("10:05"), "source": "bot"},
        {**trade, "trade_id": "T3", "order_id": "M1", "symbol": "HDFCBANK", "side": "BUY", "qty": 20, "price": 1700.0,
         "brokerage": 4.0, "stt": 6.0, "trade_ts": at("13:10")},  # no total: summed from the parts
        {**trade, "trade_id": "T4", "order_id": "M2", "symbol": "HDFCBANK", "side": "SELL", "qty": 20, "price": 1690.0,
         "total_charges": 10.0, "trade_ts": at("13:40")},
        {**trade, "trade_id": "T5", "order_id": "M3", "symbol": "INFY", "side": "SELL", "qty": 5, "price": 1500.0,
         "total_charges": 5.0, "trade_ts": at("11:00"), "product": "CNC"},
        {**trade, "trade_id": "T6", "order_id": "M4", "symbol": "TCS", "side": "SELL", "qty": 2, "price": 4000.0,
         "total_charges": 4.0, "trade_ts": at("11:10"), "product": "CNC"},
    ], day)
    store.replace("exec_orders", [
        {"cid": "C1", "broker_id": "O1", "symbol": "RELIANCE", "side": "BUY", "qty": 10, "role": "entry",
         "ref_price": 1449.5, "order_type": "MARKET", "placed_ts": at("09:21")},
        {"cid": "C2", "broker_id": "O2", "symbol": "RELIANCE", "side": "SELL", "qty": 10, "role": "target",
         "ref_price": 1460.0, "order_type": "LIMIT", "placed_ts": at("09:22")},
        {"cid": "P1", "broker_id": "PAPER-1", "symbol": "SBIN", "side": "BUY", "qty": 100, "role": "entry",
         "ref_price": 799.0, "mode": "paper", "placed_ts": at("11:00")},
    ], day)
    store.append("exec_fills", [
        {"cid": "P1", "broker_id": "PAPER-1", "symbol": "SBIN", "side": "BUY", "qty": 100, "price": 800.0,
         "ts": at("11:00"), "mode": "paper", "est_charges": 20.0, "product": "INTRADAY", "platform": "dhan",
         "role": "entry"},
        {"cid": "P2", "broker_id": "PAPER-2", "symbol": "SBIN", "side": "SELL", "qty": 100, "price": 805.0,
         "ts": at("11:30"), "mode": "paper", "est_charges": 21.0, "product": "INTRADAY", "platform": "dhan",
         "role": "squareoff"},
        {"cid": "C1", "broker_id": "O1", "symbol": "RELIANCE", "side": "BUY", "qty": 10, "price": 1450.0,
         "ts": at("09:21"), "mode": "live", "product": "INTRADAY", "platform": "dhan"},  # real: from the trade book
    ], day)
    store.replace("holdings", [{"platform": "dhan", "symbol": "INFY", "qty": 5, "avg_cost": 1400.0}], day)

    async with team:
        c = team.client
        h = await c.request(topics.cmd("journal", "recompute"), {}, timeout=20)
        assert h["all"]["trades"] == 3 and h["all"]["wins"] == 2 and h["all"]["net_pnl"] == 86.0 - 220.0 + 495.0
        assert h["unmatched"] == 1 and h["paper_today"]["net_pnl"] == 459.0 and h["today"]["trades"] == 3
        reader = TableReader(team.paths.tables)
        rows = {r["symbol"]: r for r in reader.table("perf_trades").to_dicts()}
        rel = rows["RELIANCE"]
        assert rel["source"] == "bot" and rel["exit_reason"] == "target" and rel["slippage"] == 5.0
        assert rel["net"] == 86.0 and rel["holding_s"] == 44 * 60
        assert rows["HDFCBANK"]["charges"] == 20.0 and rows["HDFCBANK"]["net"] == -220.0
        assert rows["INFY"]["avg_entry"] == 1400.0 and rows["TCS"]["status"] == "unmatched"
        assert rows["SBIN"]["book"] == "paper" and rows["SBIN"]["slippage"] == 100.0 and rows["SBIN"]["exit_reason"] == \
            "square-off"
        summ = {(r["scope"], r["source"]): r for r in reader.table("perf_summary").to_dicts()}
        assert summ[("today", "bot")]["trades"] == 1 and summ[("all", "manual")]["trades"] == 2
        assert summ[("all", "paper")]["net_pnl"] == 459.0 and summ[("all", "real")]["losing_streak"] == 1

        key = rel["trade_key"]
        r = await c.request(topics.cmd("journal", "annotate"), {"trade_key": key, "tags": "Breakout, news",
                                                                "note": "clean entry"})
        assert r["saved"]
        bad = await c.request(topics.cmd("journal", "annotate"), {"trade_key": "nope", "tags": "x"})
        assert not bad["saved"] and "nope" in bad["error"]
        tags = reader.query("SELECT bucket, net_pnl FROM perf_breakdown WHERE scope = 'all' AND source = 'real' "
                            "AND dimension = 'tag' ORDER BY sort").to_dicts()
        assert {"bucket": "breakout", "net_pnl": 86.0} in tags
        await team.stop("journal")  # notes live in journal.db: a restart keeps them
        await team.start("journal")
        await c.request(topics.cmd("journal", "recompute"), {}, timeout=20)
        again = reader.query("SELECT tags, note FROM perf_trades WHERE trade_key = ?", [key]).to_dicts()[0]
        assert again == {"tags": "breakout,news", "note": "clean entry"}
        notes = await c.request(topics.cmd("journal", "notes"), {})
        assert notes["notes"][0]["tags"] == ["breakout", "news"]
        st = await c.request(topics.cmd("journal", "stats"), {"scope": "month", "source": "real"})
        assert st["stats"]["trades"] == 3
        clear = await c.request(topics.cmd("journal", "annotate"), {"items": [{"trade_key": key, "tags": "", "note": ""}]})
        assert clear["saved"] and (await c.request(topics.cmd("journal", "notes"), {}))["notes"] == []
        events = await c.recent(topics.JOURNAL_UPDATED, 5)
        assert events and events[-1].data["all"]["trades"] == 3
