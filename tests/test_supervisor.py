"""Supervisor with real bot processes on the SQLite bus: start, heartbeats, crash restart, graceful stop."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import time

import pytest

from tradingbots.core import topics
from tradingbots.core.bus import make_bus
from tradingbots.core.settings import parse_settings, write_settings_file
from tradingbots.runner import SimArgs, build_context
from tradingbots.supervisor import KV_SUPERVISOR, SupervisorBot

from .helpers import wait_for
from .team import next_weekday_at

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals to simulate a crash")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_supervisor_starts_restarts_and_stops_bot_processes(tmp_path):
    write_settings_file(tmp_path / "config" / "settings.toml",
                        parse_settings({"platform": {"name": "simulated"}, "market_data": {"snapshot_interval_s": 60}}))
    sim = SimArgs(start=next_weekday_at("09:20"), speed=20.0, wall_ref=time.time(), port=free_port())
    ctx = build_context("supervisor", tmp_path, sim)
    await ctx.bus.connect()
    sup = SupervisorBot(ctx, ["sim_market", "config", "market_data"], sim=sim, dashboard=False)
    task = asyncio.create_task(sup.run())
    client = make_bus("sqlite", "test", data_dir=ctx.paths.data)
    await client.connect()
    try:
        async def all_up():
            st = await client.kv_get(KV_SUPERVISOR)
            return bool(st) and len(st["bots"]) == 3 and all(
                b["state"] == "running" and b["heartbeat_age_s"] is not None and b["heartbeat_age_s"] < 15
                for b in st["bots"])

        assert await wait_for(all_up, 90), await client.kv_get(KV_SUPERVISOR)
        # the real processes cooperate: simulated market -> config (token) -> market data (quotes)
        assert await wait_for(lambda: client.kv_get(topics.KV_QUOTES), 60)
        md = sup.children["market_data"]
        pid = md.proc.pid
        os.kill(pid, signal.SIGKILL)  # a crash
        assert await wait_for(lambda: md.state == "running" and md.proc is not None and md.proc.pid != pid, 45), md
        assert md.restarts == 1 and md.last_exit == -signal.SIGKILL
        alerts = []
        await client.ensure_group("t", [topics.ALERT], start="earliest")
        for m in await client.consume("t", [topics.ALERT], batch=500, block_s=0.5):
            alerts.append(m.data)
        assert any(a["key"] == "sup-market_data" and "exited with code" in a["text"] for a in alerts)
        r = await client.request(topics.cmd("supervisor", "restart"), {"bot": "config"}, timeout=10)
        assert r["restarted"]
        cfg = sup.children["config"]
        assert await wait_for(lambda: cfg.restarts == 1 and cfg.state == "running", 45)
        assert cfg.crashes == type(cfg.crashes)()  # a requested restart is not counted as a crash
        st = await client.request(topics.cmd("supervisor", "status"), {}, timeout=10)
        assert {b["name"] for b in st["bots"]} == {"sim_market", "config", "market_data"}
    finally:
        sup.stop()
        await asyncio.wait_for(task, 120)
        await client.close()
        await ctx.bus.close()
    assert all(c.state == "stopped" and c.proc is None for c in sup.children.values())
    assert all(c.last_exit == 0 for c in sup.children.values()), {n: c.last_exit for n, c in sup.children.items()}
