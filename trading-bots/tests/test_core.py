"""Bus contract (memory / sqlite / real redis), bot base class, settings, and Parquet tables."""

from __future__ import annotations

import asyncio
import time
from datetime import date

import polars as pl
import pytest

from tradingbots.core import topics
from tradingbots.core.bot import Bot
from tradingbots.core.bus import BusTimeout, MemoryBus, RedisBus, SqliteBus
from tradingbots.core.settings import (Settings, SettingsError, deep_merge, diff, load_settings_file, parse_settings,
                                       write_settings_file)
from tradingbots.core.tables_io import TableReader, TableStore

from .helpers import make_ctx, wait_for


@pytest.fixture(params=["memory", "sqlite", "redis"])
async def bus_pair(request, tmp_path):
    """Two clients of the same bus (like two bot processes)."""
    if request.param == "memory":
        a, b = MemoryBus("a", "t"), MemoryBus("b", "t")
    elif request.param == "sqlite":
        a, b = SqliteBus(tmp_path / "bus.db", "a"), SqliteBus(tmp_path / "bus.db", "b")
    else:
        url = request.getfixturevalue("redis_url")
        a, b = RedisBus(url, "a"), RedisBus(url, "b")
        await a.connect()
        await a.r.flushdb()
    for x in (a, b):
        await x.connect()
    yield a, b
    for x in (a, b):
        await x.close()


async def test_bus_publish_consume_ack_and_resume(bus_pair):
    a, b = bus_pair
    await b.ensure_group("g1", ["t.x", "t.y"])
    await a.publish("t.x", {"n": 1})
    await a.publish("t.z", {"n": 99})  # not subscribed
    await a.publish("t.y", {"n": 2, "nested": {"k": [1, 2]}})
    msgs = await b.consume("g1", ["t.x", "t.y"], block_s=1.0)
    assert [m.data["n"] for m in msgs] == [1, 2] and msgs[0].source == "a" and msgs[1].data["nested"] == {"k": [1, 2]}
    assert {m.topic for m in msgs} == {"t.x", "t.y"}
    # not acked yet -> a restarted consumer (new client object) gets them again
    await b.ack("g1", msgs[:1])
    await a.publish("t.x", {"n": 3})
    again = await b.consume("g1", ["t.x", "t.y"], block_s=1.0)
    assert 3 in [m.data["n"] for m in again]
    await b.ack("g1", again)
    assert await b.consume("g1", ["t.x", "t.y"], block_s=0.2) == []


async def test_bus_groups_fan_out_and_latest_start(bus_pair):
    a, b = bus_pair
    await a.publish("t.f", {"n": 0})  # before either group exists
    await b.ensure_group("g1", ["t.f"])
    await b.ensure_group("g2", ["t.f"], start="earliest")
    await a.publish("t.f", {"n": 1})
    g1 = await b.consume("g1", ["t.f"], block_s=1.0)
    g2 = await b.consume("g2", ["t.f"], block_s=1.0)
    assert [m.data["n"] for m in g1] == [1]
    assert [m.data["n"] for m in g2] == [0, 1]


async def test_bus_blocking_wakeup(bus_pair):
    a, b = bus_pair
    await b.ensure_group("g", ["t.w"])

    async def later():
        await asyncio.sleep(0.2)
        await a.publish("t.w", {"ok": True})

    task = asyncio.create_task(later())
    msgs = await b.consume("g", ["t.w"], block_s=3.0)
    await task
    assert msgs and msgs[0].data == {"ok": True}


async def test_bus_kv_ttl_and_keys(bus_pair):
    a, b = bus_pair
    await a.kv_set("hb:x", {"s": 1})
    await a.kv_set("hb:y", {"s": 2}, ttl=0.3)
    await a.kv_set("other", 5)
    assert await b.kv_get("hb:x") == {"s": 1} and await b.kv_get("other") == 5
    assert await b.kv_keys("hb:") == ["hb:x", "hb:y"]
    await asyncio.sleep(0.5)
    assert await b.kv_get("hb:y") is None and await b.kv_get("missing", "dflt") == "dflt"
    await a.kv_delete("hb:x")
    assert await b.kv_keys("hb:") == []


async def test_bus_request_reply(bus_pair):
    a, b = bus_pair
    await b.ensure_group("server", ["cmd.server.ping"])

    async def serve():
        msgs = await b.consume("server", ["cmd.server.ping"], block_s=3.0)
        for m in msgs:
            await b.send_reply(m.data["_corr"], {"pong": m.data["x"] * 2})
        await b.ack("server", msgs)

    t = asyncio.create_task(serve())
    assert await a.request("cmd.server.ping", {"x": 21}, timeout=5) == {"pong": 42}
    await t
    with pytest.raises(BusTimeout):
        await a.request("cmd.nobody.home", {}, timeout=1)


async def test_bus_trim(bus_pair):
    a, b = bus_pair
    await a.publish("t.old", {"n": 1})
    await asyncio.sleep(0.05)
    removed = await a.trim(retention_s=0.0)
    assert removed >= 1 or isinstance(a, RedisBus)  # redis trims approximately


# ---- bot base class ----------------------------------------------------------------------------------


class EchoBot(Bot):
    name = "echo"
    heartbeat_s = 0.2

    def configure(self):
        self.seen: list[int] = []
        self.ticks = 0
        self.settings_changes = 0
        self.on("t.in", self.handle)
        self.command("double", self.double)
        self.command("boom", self.boom)
        self.command("slow", self.slow, background=True)
        self.every(0.1, self.job, name="ticker", run_at_start=True)

    async def handle(self, msg):
        if msg.data.get("bad"):
            raise ValueError("bad message")
        self.seen.append(msg.data["n"])

    async def double(self, data):
        return {"value": data["x"] * 2}

    async def boom(self, data):
        raise RuntimeError("nope")

    async def slow(self, data):
        await asyncio.sleep(1.0)
        return {"slept": True}

    async def job(self):
        self.ticks += 1

    async def on_settings(self, old, new):
        self.settings_changes += 1

    def details(self):
        return {"seen": len(self.seen)}


async def test_bot_handlers_commands_jobs_heartbeat_and_isolation(tmp_path):
    ctx = make_ctx(tmp_path, bus=MemoryBus("echo"))
    bot = EchoBot(ctx)
    client = MemoryBus("client")
    task = asyncio.create_task(bot.run())
    assert await wait_for(lambda: bot.status == "ok")
    await client.publish("t.in", {"n": 1})
    await client.publish("t.in", {"bad": True})  # handler error must not stop the bot
    await client.publish("t.in", {"n": 2})
    assert await wait_for(lambda: bot.seen == [1, 2])
    assert bot.counters["errors"] == 1 and "bad message" in bot.last_error
    assert await client.request(topics.cmd("echo", "double"), {"x": 4}) == {"ok": True, "value": 8}
    r = await client.request(topics.cmd("echo", "boom"), {})
    assert r["ok"] is False and "nope" in r["error"]
    assert await wait_for(lambda: bot.ticks >= 3)
    hb = await client.kv_get(topics.kv_heartbeat("echo"))
    assert hb["status"] == "ok" and hb["details"]["seen"] == 2 and hb["counters"]["commands"] == 2
    # a background command does not hold up the bot's other messages
    slow = asyncio.create_task(client.request(topics.cmd("echo", "slow"), {}, timeout=5))
    await asyncio.sleep(0.1)
    t0 = time.monotonic()
    assert (await client.request(topics.cmd("echo", "double"), {"x": 1}))["value"] == 2
    assert time.monotonic() - t0 < 0.8
    assert await slow == {"ok": True, "slept": True}
    # settings change through the config KV + event
    s = Settings().model_dump(mode="json")
    s["ai"]["effort"] = "max"
    await client.kv_set(topics.KV_CONFIG, {"version": 2, "settings": s})
    await client.publish(topics.CONFIG_CHANGED, {"version": 2, "changed": ["ai.effort"]})
    assert await wait_for(lambda: bot.settings.ai.effort == "max" and bot.settings_changes == 1)
    assert bot.settings_version == 2
    bot.stop()
    await asyncio.wait_for(task, 5)
    assert (await client.kv_get(topics.kv_heartbeat("echo")))["status"] == "stopped"


# ---- settings --------------------------------------------------------------------------------------------


def test_settings_validation_and_roundtrip(tmp_path):
    s = Settings()
    assert s.platform.name == "simulated" and s.benchmark().symbol == "NIFTY" and len(s.stocks()) == 3
    with pytest.raises(SettingsError) as e:
        parse_settings({"budget": {"daily_capital_limit": 5}})
    assert "unknown setting" in str(e.value)
    with pytest.raises(SettingsError) as e:
        parse_settings({"watchlist": [{"symbol": "X", "segment": "NOPE"}]})
    assert "unknown segment" in str(e.value)
    with pytest.raises(SettingsError):
        parse_settings({"watchlist": [{"symbol": "NIFTY", "segment": "IDX_I"}]})  # index needs an id
    with pytest.raises(SettingsError):
        parse_settings({"schedule": {"ai_runs": ["25:00"]}})
    p = tmp_path / "config" / "settings.toml"
    s2 = parse_settings(deep_merge(s.to_dict(), {"ai": {"effort": "max"}, "platform": {"name": "dhan"}}))
    write_settings_file(p, s2)
    assert load_settings_file(p) == s2
    d = diff(s.to_dict(), s2.to_dict())
    assert "ai.effort: 'high' -> 'max'" in d and any(x.startswith("platform.name") for x in d)
    assert load_settings_file(tmp_path / "missing.toml") == Settings()


# ---- tables ---------------------------------------------------------------------------------------------


def test_table_store_append_replace_compact_and_query(tmp_path):
    store, reader = TableStore(tmp_path), TableReader(tmp_path)
    assert reader.count("bars_1m") == 0  # empty view with the declared schema
    d1, d2 = date(2026, 11, 4), date(2026, 11, 5)
    rows = [{"symbol": "A", "segment": "NSE_EQ", "security_id": 1, "tf": 60, "start_ts": 1.0, "open": 1, "high": 2,
             "low": 0.5, "close": 1.5, "volume": 10, "junk": "ignored"}]
    store.append("bars_1m", rows, day=d1)
    store.append("bars_1m", [dict(rows[0], start_ts=2.0)], day=d1)
    store.append("bars_1m", pl.DataFrame([dict(rows[0], start_ts=3.0, date=d2)]))
    df = reader.query("SELECT date, start_ts, open FROM bars_1m ORDER BY start_ts")
    assert df["start_ts"].to_list() == [1.0, 2.0, 3.0] and df["date"].to_list() == [d1, d1, d2]
    assert df["open"].dtype == pl.Float64 and "junk" not in reader.table("bars_1m").columns
    assert store.compact("bars_1m", d1) == 2 and reader.count("bars_1m") == 3
    assert len(list((tmp_path / "bars_1m" / "date=2026-11-04").glob("*.parquet"))) == 1
    # replace semantics
    store.replace("positions", [{"platform": "dhan", "symbol": "A", "net_qty": 5}], partition_value=d1)
    store.replace("positions", [{"platform": "dhan", "symbol": "A", "net_qty": 7}], partition_value=d1)
    assert reader.query("SELECT net_qty FROM positions")["net_qty"].to_list() == [7]
    store.replace("candles_daily", [{"segment": "NSE_EQ", "day": d1, "close": 10.0}], partition_value="RELIANCE")
    store.replace("candles_daily", [{"segment": "NSE_EQ", "day": d1, "close": 20.0}], partition_value="M&M")
    got = reader.query("SELECT symbol, close FROM candles_daily ORDER BY symbol")
    assert got.to_dicts() == [{"symbol": "M&M", "close": 20.0}, {"symbol": "RELIANCE", "close": 10.0}]
    store.replace("history_metrics", [{"symbol": "A", "rsi14": 55.0}])
    assert reader.table("history_metrics", where="symbol = ?", params=["A"])["rsi14"][0] == 55.0
    assert store.partitions("bars_1m") == ["2026-11-04", "2026-11-05"]
    store.drop_partition("bars_1m", "2026-11-05")
    assert reader.count("bars_1m") == 2
    assert "bars_1m" in reader.available()


async def test_bus_recent_is_a_read_only_peek(bus_pair):
    a, b = bus_pair
    for i in range(5):
        await a.publish("t.alert", {"n": i})
    await a.publish("t.other", {"n": 99})
    last3 = await b.recent("t.alert", 3)
    assert [m.data["n"] for m in last3] == [2, 3, 4] and all(m.topic == "t.alert" for m in last3)
    assert [m.data["n"] for m in await b.recent("t.alert", 100)] == [0, 1, 2, 3, 4]
    assert await b.recent("t.none", 10) == [] and await b.recent("t.alert", 0) == []
    await b.ensure_group("g", ["t.alert"], start="earliest")  # peeking never moved a cursor
    assert len(await b.consume("g", ["t.alert"], block_s=0.5)) == 5
