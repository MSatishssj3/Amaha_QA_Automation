"""Platform adapter normalisers, config bot, market-data bot against the simulated market."""

from __future__ import annotations

from datetime import date

from tradingbots.core import topics
from tradingbots.platforms import normalize as N

from .helpers import make_settings, wait_for
from .team import Team


def test_normalisers():
    t = N.trade_row({"exchangeTradeId": "T1", "orderId": "52", "tradingSymbol": "RELIANCE", "exchangeSegment": "NSE_EQ",
                     "securityId": "2885", "transactionType": "buy", "tradedQuantity": "10", "tradedPrice": "1450.5",
                     "productType": "INTRADAY", "brokerageCharges": 4.35, "stt": "0", "exchangeTransactionCharges": 0.43,
                     "sebiTax": 0.01, "stampDuty": 0.44, "serviceTax": 0.86, "createTime": "2026-11-04 10:15:00"},
                    "dhan")
    assert t["side"] == "BUY" and t["qty"] == 10 and t["value"] == 14505.0 and t["total_charges"] == 6.09
    assert t["date"] == date(2026, 11, 4) and t["trade_id"] == "T1"
    led = N.ledger_row({"narration": "FUNDS ADDED VIA UPI", "voucherdate": "Nov 04, 2026", "debit": "0.00",
                        "credit": "50,000.00", "runbal": "50000.00"}, "dhan")
    assert led["category"] == "deposit" and led["credit"] == 50000.0 and led["date"] == date(2026, 11, 4)
    assert N.ledger_category("Charges RELIANCE BUY 10", 6.0, 0) == "charges"
    assert N.ledger_category("FUNDS WITHDRAWN", 1000, 0) == "withdrawal"
    assert N.ledger_category("Intraday settlement INFY", 0, 50) == "settlement"
    assert N.ist_date("04-11-2026") == date(2026, 11, 4) and N.ist_date("2026-11-04") == date(2026, 11, 4)
    assert N.ist_ts("garbage") is None and N.num("1,234.5") == 1234.5 and N.inum("NA", 7) == 7
    pos = N.position_row({"tradingSymbol": "INFY", "exchangeSegment": "NSE_EQ", "securityId": "1594",
                          "productType": "CNC", "netQty": 5, "realizedProfit": "12.5"}, "dhan", 1.0)
    assert pos["net_qty"] == 5 and pos["realized"] == 12.5 and pos["product"] == "CNC"


async def test_config_bot_versions_validation_rollback_and_file_import(tmp_path):
    team = Team(tmp_path, ["config"])
    async with team:
        c = team.client
        cur = await c.request(topics.cmd("config", "get"), {})
        assert cur["version"] == 1 and cur["settings"]["platform"]["name"] == "simulated"
        assert (tmp_path / "config" / "settings.toml").exists()
        bad = await c.request(topics.cmd("config", "update"), {"patch": {"budget": {"max_trades_per_day": -1}}})
        assert bad["applied"] is False and bad["errors"]
        typo = await c.request(topics.cmd("config", "update"), {"patch": {"ai": {"modle": "x"}}})
        assert "unknown setting" in typo["errors"][0]
        ok = await c.request(topics.cmd("config", "update"), {"patch": {"ai": {"effort": "max"}}, "author": "test"})
        assert ok["applied"] and ok["version"] == 2 and ok["changes"] == ["ai.effort: 'high' -> 'max'"]
        kv = await c.kv_get(topics.KV_CONFIG)
        assert kv["version"] == 2 and kv["settings"]["ai"]["effort"] == "max"
        sysch = await c.request(topics.cmd("config", "update"), {"patch": {"system": {"bus_retention_hours": 12}}})
        assert sysch["restart_required"]
        same = await c.request(topics.cmd("config", "update"), {"patch": {"ai": {"effort": "max"}}})
        assert same["applied"] is False and same["changes"] == []
        hist = await c.request(topics.cmd("config", "history"), {})
        assert [v["version"] for v in hist["versions"]] == [3, 2, 1] and hist["versions"][1]["author"] == "test"
        rb = await c.request(topics.cmd("config", "rollback"), {"version": 1})
        assert rb["applied"] and (await c.kv_get(topics.KV_CONFIG))["settings"]["ai"]["effort"] == "high"
    # hand-edit the TOML file -> imported as a new version on the next start
    p = tmp_path / "config" / "settings.toml"
    p.write_text(p.read_text().replace('effort = "high"', 'effort = "low"'))
    team2 = Team(tmp_path, ["config"], namespace="team2")
    async with team2:
        cur = await team2.client.request(topics.cmd("config", "get"), {})
        assert cur["settings"]["ai"]["effort"] == "low" and cur["version"] == 5


async def test_market_data_against_simulated_market(tmp_path):
    team = Team(tmp_path, ["sim_market", "config", "market_data"], speed=30)
    async with team:
        c = team.client
        md = team.bots["market_data"]
        assert await wait_for(lambda: md.stream is not None and md.stream.connected, 20), md.last_error
        tok = await c.kv_get(topics.KV_TOKEN)
        assert tok["platform"] == "simulated" and tok["token"] and tok["endpoints"]["rest_base"].startswith("http://")
        await c.ensure_group("probe", [topics.MARKET_QUOTES, topics.MARKET_BARS, topics.ACCOUNT_HOLDINGS,
                                       topics.ACCOUNT_LEDGER, topics.ACCOUNT_TRADES, topics.MARKET_HISTORY,
                                       topics.MARKET_STATUS], start="earliest")
        seen: dict[str, list] = {}

        async def drain():
            msgs = await c.consume("probe", [topics.MARKET_QUOTES, topics.MARKET_BARS, topics.ACCOUNT_HOLDINGS,
                                             topics.ACCOUNT_LEDGER, topics.ACCOUNT_TRADES, topics.MARKET_HISTORY,
                                             topics.MARKET_STATUS], block_s=0.2)
            for m in msgs:
                seen.setdefault(m.topic, []).append(m.data)
            await c.ack("probe", msgs)
            return topics.MARKET_BARS in seen and topics.MARKET_QUOTES in seen

        assert await wait_for(drain, 30), list(seen)
        q = seen[topics.MARKET_QUOTES][-1]["quotes"][0]
        assert q["ltp"] > 0 and q["symbol"] in ("NIFTY", "RELIANCE", "HDFCBANK", "INFY")
        bar = seen[topics.MARKET_BARS][0]["bars"][0]
        assert bar["tf"] == 60 and bar["high"] >= bar["low"] > 0
        holds = seen[topics.ACCOUNT_HOLDINGS][0]["rows"]
        assert {h["symbol"] for h in holds} == {"INFY", "HDFCBANK"}
        assert seen[topics.ACCOUNT_LEDGER][0]["rows"][0]["category"] == "deposit"
        kvq = await c.kv_get(topics.KV_QUOTES)
        assert "NSE_EQ:RELIANCE" in kvq and "IDX_I:NIFTY" in kvq
        # history on request
        r = await c.request(topics.cmd("market_data", "fetch_history"),
                            {"symbols": [{"symbol": "RELIANCE", "segment": "NSE_EQ"},
                                         {"symbol": "NIFTY", "segment": "IDX_I", "security_id": 13},
                                         {"symbol": "NOPE", "segment": "NSE_EQ"}],
                             "from": "2026-01-01", "to": "2026-12-31"}, timeout=20)
        assert r["fetched"]["RELIANCE"] > 100 and "NOPE" in r["errors"]
        assert await wait_for(drain, 5) and topics.MARKET_HISTORY in seen
        # trade history backfill for the tax bot: 60-day windows, paced, in the background
        ft = await c.request(topics.cmd("market_data", "fetch_trades"), {"from": "2026-01-01", "to": "2026-04-30"},
                             timeout=30)
        assert ft["errors"] == [] and ft["windows"] == 2 and ft["fetched"] >= 0
        bad = await c.request(topics.cmd("market_data", "fetch_trades"), {"from": "2026-05-01", "to": "2026-04-01"})
        assert bad["error"] == "from is after to"
        # platform switch / watchlist change -> reconnect with the new list
        upd = await c.request(topics.cmd("config", "update"),
                              {"patch": {"watchlist": [{"symbol": "INFY"}, {"symbol": "TYPO123"}]}})
        assert upd["applied"]
        assert await wait_for(lambda: md.stream is not None and md.stream.connected and len(md.instruments) == 1, 20)
        assert md.problems and "TYPO123" in md.problems[0]
        async def hb_instruments():
            hb = await c.kv_get(topics.kv_heartbeat("market_data"))
            return hb and hb["details"]["instruments"] == 1

        assert await wait_for(hb_instruments, 12)


async def test_config_bot_dhan_platform_without_credentials_alerts(tmp_path):
    s = make_settings(tmp_path, platform={"name": "dhan"})
    team = Team(tmp_path, ["config"], settings=s)
    async with team:
        st = await team.client.request(topics.cmd("config", "token_status"), {})
        assert st["platform"] == "dhan" and "no valid Dhan access token" in st["error"] or st["error"]


async def test_market_data_records_raw_frames_for_replay(tmp_path):
    from dhanbot.dhan.packets import Tick, parse_frame
    from dhanbot.recorder import iter_frames

    s = make_settings(tmp_path, platform={"name": "simulated"}, market_data={"record_raw": True})
    team = Team(tmp_path, ["sim_market", "config", "market_data"], settings=s, speed=30)
    async with team:
        md = team.bots["market_data"]
        assert await wait_for(lambda: md.recorder is not None and md.recorder.frames > 20, 40)
        day = md._rec_day
        await md.recorder.flush()
        # switching the setting off stops recording without a reconnect
        up = await team.client.request(topics.cmd("config", "update"), {"patch": {"market_data": {"record_raw": False}}})
        assert up["applied"]
        assert await wait_for(lambda: md.recorder is None, 20)
    path = tmp_path / "data" / "ticks" / f"{day}.bin"
    frames = list(iter_frames(path))
    assert len(frames) > 20
    ticks = [p for _, f in frames for p in parse_frame(f) if isinstance(p, Tick)]
    assert ticks, "recorded frames parse back into ticks"
    stamps = [ns for ns, _ in frames]
    assert stamps == sorted(stamps) and abs(stamps[-1] / 1e9 - team.clock.time()) < 3600  # team clock, not wall
