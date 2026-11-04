"""Engine in LIVE mode against the local mock Dhan (real REST + WebSockets, accelerated SimClock)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from dhanbot.clock import SimClock, epoch_at, ist_datetime
from dhanbot.config import load_config
from dhanbot.engine import Engine
from dhanbot.errors import PreflightError
from dhanbot.models import OrderStatus
from dhanbot.sim.mock_dhan import MockDhan
from dhanbot.strategy import Strategy

from .helpers import wait_for

CID = "1000000001"


def sim_start(hhmm: str = "09:20:00") -> float:
    import time

    d = ist_datetime(time.time()).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return epoch_at(d, hhmm)


class FirstTick(Strategy):
    name = "first_tick"
    params = {"qty": 2, "sl_off": 0.0, "tg_off": 0.0}

    def subscriptions(self):
        return ["RELIANCE"], [("RELIANCE", "1m")]

    def on_start(self, ctx):
        self.order = None
        self.events = []
        self.recovered = None
        self.ticks = 0

    def on_recover(self, ctx, positions):
        self.recovered = [p.qty for p in positions]

    def on_tick(self, ctx, tick):
        self.ticks += 1
        if self.order is None and self.recovered is None and ctx.can_enter() and ctx.quote("RELIANCE").ask:
            px = tick.ltp
            self.order = ctx.buy("RELIANCE", self.p.qty, sl=(px - self.p.sl_off) if self.p.sl_off else None,
                                 target=(px + self.p.tg_off) if self.p.tg_off else None)

    def on_order(self, ctx, ev):
        self.events.append(ev.kind)


@pytest.fixture
async def env(tmp_path):
    clock = SimClock(sim_start(), speed=20)
    mock = MockDhan(clock=clock, client_id=CID, tick_interval_s=1.0)
    mock.ips["primaryIP"] = "203.0.113.10"
    ep = await mock.start()
    engines: list[Engine] = []

    def make(strategy, **sections):
        data = {
            "mode": {"mode": "live"},
            "paths": {"data_dir": str(tmp_path / "data")},
            "instruments": [{"alias": "RELIANCE", "segment": "NSE_EQ", "trading_symbol": "RELIANCE", "mode": "full",
                             "max_qty": 100},
                            {"alias": "NIFTY", "segment": "IDX_I", "security_id": 13, "mode": "ticker"}],
            "risk": {"max_daily_loss_inr": 1000},
            "perf": {"gc_freeze": False},
        }
        for k, v in sections.items():
            data.setdefault(k, {}).update(v)
        cfg = load_config(data=data, env={"DHAN_CLIENT_ID": CID, "DHAN_ACCESS_TOKEN": mock.token}, live_requested=True)
        e = Engine(cfg, clock=clock, endpoints=ep, strategy=strategy, use_control=False, use_telegram=False)
        engines.append(e)
        return e

    yield clock, mock, make
    for e in engines:
        if not e._shutting_down and e.oms is not None:
            await e.shutdown()
    await mock.stop()


async def test_live_super_order_round_trip_with_broker_side_stop(env):
    clock, mock, make = env
    strat = FirstTick({"qty": 2, "sl_off": 5.0, "tg_off": 50.0})
    e = make(strat)
    await e.start()
    names = {c.name: c for c in e.preflight_checks}
    assert names["static_ip"].ok and names["data_plan"].ok and names["recovery"].ok
    assert mock.pnl_exit and mock.pnl_exit["loss"] == 1250.0  # server-side backstop at 1.25x
    assert await wait_for(lambda: strat.order is not None and strat.order.status is OrderStatus.FILLED, timeout=10)
    body = [r for r in mock.requests if r[1] == "/v2/super/orders"][-1][2]
    assert body["transactionType"] == "BUY" and body["quantity"] == 2 and body["correlationId"] == strat.order.cid
    assert e.positions.qty(e.resolve("RELIANCE")) == 2
    rel = mock.instrument("RELIANCE")
    mock.set_price("RELIANCE", rel.price - 10)  # Dhan's stop leg fires
    assert await wait_for(lambda: e.positions.qty(e.resolve("RELIANCE")) == 0, timeout=10)
    assert strat.order.exit_filled_qty == 2 and "fill" in strat.events
    assert e.risk.closed_trades == 1
    st = e.status()
    assert st["feed"] and st["order_ws"] and st["mode"] == "live"


async def test_live_daily_loss_halt_flattens_and_activates_dhan_killswitch(env):
    clock, mock, make = env
    strat = FirstTick({"qty": 60})
    e = make(strat, execution={"exit_mode": "none"}, risk={"broker_killswitch_on_halt": True})
    await e.start()
    assert await wait_for(lambda: strat.order is not None and strat.order.status is OrderStatus.FILLED, timeout=10)
    rel = mock.instrument("RELIANCE")
    mock.set_price("RELIANCE", rel.price - 25)  # 60 x 25 = 1500 loss > 1000 limit
    assert await wait_for(lambda: e.ks.state.value == "HALTED", timeout=10)
    assert await wait_for(lambda: e.positions.qty(e.resolve("RELIANCE")) == 0 and mock.killswitch, timeout=15)
    pos = [p for p in mock.positions.values() if p.inst.trading_symbol == "RELIANCE"][0]
    assert pos.net == 0  # flat at the broker too
    assert any("daily loss" in a.text for a in e.alerts)


async def test_restart_recovery_adopts_positions_and_orders(env):
    clock, mock, make = env
    s1 = FirstTick({"qty": 3})
    e1 = make(s1, execution={"exit_mode": "none", "flatten_on_shutdown": False})
    await e1.start()
    assert await wait_for(lambda: s1.order is not None and s1.order.status is OrderStatus.FILLED, timeout=10)
    await e1.shutdown()
    s2 = FirstTick({"qty": 3})
    e2 = make(s2, execution={"exit_mode": "none"})
    await e2.start()
    assert s2.recovered == [3]  # strategy told about the inherited position
    assert e2.positions.qty(e2.resolve("RELIANCE")) == 3
    rec = [c for c in e2.preflight_checks if c.name == "recovery"][0]
    assert "adopted 1 orders" in rec.detail and "1 open bot positions" in rec.detail
    assert s1.order.cid in e2.oms.by_cid  # the same correlationId is known again
    new_order = e2.oms.next_cid()
    assert new_order != s1.order.cid and new_order > s1.order.cid  # sequence continues from the journal
    await e2.shutdown()  # flatten_on_shutdown (default) closes it
    assert e2.positions.qty(e2.resolve("RELIANCE")) == 0


async def test_scheduled_squareoff(env):
    clock, mock, make = env
    t = ist_datetime(clock.time() + 40)
    strat = FirstTick({"qty": 2})
    e = make(strat, execution={"exit_mode": "none"},
             session={"entry_end": t.strftime("%H:%M:%S"), "squareoff_time": t.strftime("%H:%M:%S")})
    await e.start()
    assert await wait_for(lambda: strat.order is not None and strat.order.status is OrderStatus.FILLED, timeout=10)
    assert await wait_for(lambda: e.squareoff_done and e.positions.qty(e.resolve("RELIANCE")) == 0, timeout=15)
    assert e.ks.state.value == "PAUSED"


async def test_feed_drop_reconnects_and_strategy_keeps_ticking(env):
    clock, mock, make = env
    strat = FirstTick({"qty": 1})
    e = make(strat, execution={"exit_mode": "none"})
    await e.start()
    assert await wait_for(lambda: strat.ticks > 5, timeout=10)
    await mock.drop_feeds()
    await mock.drop_order_ws()
    assert await wait_for(lambda: e.feed.reconnects >= 1 and e.feed.connected, timeout=10)
    n = strat.ticks
    assert await wait_for(lambda: strat.ticks > n + 5, timeout=10)
    assert await wait_for(lambda: e.order_ws.connected, timeout=10)


async def test_preflight_blocks_live_without_static_ip(env):
    clock, mock, make = env
    mock.ips["primaryIP"] = ""
    e = make(FirstTick())
    with pytest.raises(PreflightError) as ei:
        await e.start()
    assert "static IP" in str(ei.value)
    await e.shutdown()


async def test_ip_rejected_order_halts(env):
    clock, mock, make = env
    mock.faults.ip_rejected = True
    strat = FirstTick({"qty": 1})
    e = make(strat, execution={"exit_mode": "none"})
    await e.start()
    assert await wait_for(lambda: strat.order is not None and strat.order.status is OrderStatus.REJECTED, timeout=10)
    assert await wait_for(lambda: e.ks.state.value == "HALTED", timeout=5)
    assert "IP" in e.ks.reason
