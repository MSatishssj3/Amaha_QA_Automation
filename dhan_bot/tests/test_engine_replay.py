"""Full engine in replay mode (VirtualClock + PaperBroker): same code path as live, no network."""

from __future__ import annotations

from datetime import date

import pytest

from dhanbot.clock import IST_OFFSET_S, epoch_at
from dhanbot.config import load_config
from dhanbot.dhan import packets as P
from dhanbot.replay import run_replay, synth_frames, synth_instrument_store
from dhanbot.strategy import Strategy
from dhanbot.strategy.examples.ema_cross import EmaCross

DAY = date(2026, 11, 4)  # Wednesday
RELIANCE = (1, 2885)


def make_cfg(tmp_path, **sections):
    data = {
        "paths": {"data_dir": str(tmp_path / "data")},
        "instruments": [{"alias": "RELIANCE", "segment": "NSE_EQ", "trading_symbol": "RELIANCE", "mode": "full",
                         "max_qty": 100},
                        {"alias": "NIFTY", "segment": "IDX_I", "security_id": 13, "mode": "ticker"}],
        "risk": {"max_daily_loss_inr": 1000, "max_order_value_inr": 200000, "max_gross_exposure_inr": 500000},
        "paper": {"slippage_ticks": 0},
        "perf": {"gc_freeze": False},
    }
    for k, v in sections.items():
        data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.__setitem__(k, v)
    return load_config(data=data, env={})


class BuyOnce(Strategy):
    name = "buy_once"
    params = {"symbol": "RELIANCE", "qty": 2, "at": "09:20", "sl_pct": 2.0, "target_pct": 50.0}

    def subscriptions(self):
        return [self.p.symbol], [(self.p.symbol, "1m")]

    def on_start(self, ctx):
        self.order = None
        self.events = []
        self.recovered = None

    def on_bar(self, ctx, bar):
        if self.order is None and ctx.now().strftime("%H:%M") >= self.p.at:
            self.order = ctx.buy(self.p.symbol, self.p.qty, sl=bar.close * (1 - self.p.sl_pct / 100),
                                 target=bar.close * (1 + self.p.target_pct / 100))

    def on_order(self, ctx, ev):
        self.events.append(ev.kind)


def frames_at(prices: list[tuple[str, float]], seg=1, sid=2885) -> list[tuple[int, bytes]]:
    out = []
    for hhmmss, px in prices:
        t = epoch_at(DAY, hhmmss)
        f = P.build_full(seg, sid, px, int(t) + IST_OFFSET_S, volume=1000,
                         bids=[(round(px - 0.1, 2), 500, 5)] * 5, asks=[(round(px + 0.1, 2), 500, 5)] * 5)
        out.append((int(t * 1e9), f))
    return out


def ticks(start: str, end: str, px: float, step: int = 1) -> list[tuple[str, float]]:
    t0, t1 = epoch_at(DAY, start), epoch_at(DAY, end)
    from dhanbot.clock import ist_datetime

    out = []
    t = t0
    while t <= t1:
        out.append((ist_datetime(t).strftime("%H:%M:%S"), px))
        t += step
    return out


@pytest.fixture(scope="module")
def store():
    return synth_instrument_store(DAY)


async def test_full_day_super_order_then_squareoff(tmp_path, store):
    cfg = make_cfg(tmp_path)
    strat = BuyOnce()
    frames = synth_frames(["RELIANCE", "NIFTY"], DAY, minutes=375, step_s=2.0)
    report = await run_replay(cfg, frames, instruments=store, strategy=strat)
    assert strat.order is not None and strat.order.kind.value == "SUPER"
    assert strat.order.filled_qty == 2 and "fill" in strat.events
    assert report["open_positions"] == []  # squared off at 15:10
    assert report["trades"]["closed"] == 1
    assert report["state"] in ("PAUSED", "RUNNING")
    stages = {s["stage"] for s in report["latency"]}
    assert {"frame_parse", "tick_to_submit"} <= stages
    assert report["ltt_mode"] == "ist_shifted"
    assert (tmp_path / "data" / "journal.db").exists()


async def test_example_strategy_runs_a_day_deterministically(tmp_path, store):
    params = {"symbol": "RELIANCE", "timeframe": "1m", "fast": 5, "slow": 13, "qty": 1, "stop_pct": 0.3,
              "target_pct": 0.6}
    frames = synth_frames(["RELIANCE"], DAY, minutes=375, step_s=2.0, seed=11)
    r1 = await run_replay(make_cfg(tmp_path / "a"), frames, instruments=store, strategy=EmaCross(params))
    r2 = await run_replay(make_cfg(tmp_path / "b"), frames, instruments=store, strategy=EmaCross(params))
    assert r1["fills"] > 0 and r1["trades"]["closed"] > 0
    assert (r1["pnl"], r1["fills"], r1["trades"]) == (r2["pnl"], r2["fills"], r2["trades"])
    assert r1["open_positions"] == [] and r1["strategy_errors"] == 0


class BuyThenRetry(Strategy):
    name = "buy_retry"
    params = {}

    def subscriptions(self):
        return ["RELIANCE"], []

    def on_start(self, ctx):
        self.orders = []

    def on_tick(self, ctx, tick):
        hhmm = ctx.now().strftime("%H:%M:%S")
        if not self.orders and hhmm >= "09:16:05":
            self.orders.append(ctx.buy("RELIANCE", 50))
        elif len(self.orders) == 1 and hhmm >= "09:20:00":
            self.orders.append(ctx.buy("RELIANCE", 1))


async def test_daily_loss_limit_halts_and_flattens(tmp_path, store):
    cfg = make_cfg(tmp_path, execution={"exit_mode": "none"})
    strat = BuyThenRetry()
    frames = frames_at(ticks("09:16:00", "09:17:00", 1450.0) + ticks("09:17:01", "09:21:00", 1420.0))
    report = await run_replay(cfg, frames, instruments=store, strategy=strat)
    assert strat.orders[0].filled_qty == 50
    assert report["state"] == "HALTED" and "daily loss" in report["state_reason"]
    assert report["open_positions"] == []  # flattened on halt
    assert report["pnl"]["net"] < -1000
    retry = strat.orders[1]
    assert retry.status.value == "REJECTED" and "killswitch" in retry.reason


class Bracketed(Strategy):
    name = "bracketed"
    params = {}

    def subscriptions(self):
        return ["RELIANCE"], []

    def on_start(self, ctx):
        self.entry = None

    def on_tick(self, ctx, tick):
        if self.entry is None and ctx.now().strftime("%H:%M:%S") >= "09:16:05":
            self.entry = ctx.buy("RELIANCE", 5, sl=1440.0, target=1460.0)


async def test_bot_managed_bracket_target(tmp_path, store):
    cfg = make_cfg(tmp_path, execution={"exit_mode": "bot_managed"})
    strat = Bracketed()
    frames = frames_at(ticks("09:16:00", "09:17:00", 1450.0) + ticks("09:17:01", "09:18:00", 1461.0))
    report = await run_replay(cfg, frames, instruments=store, strategy=strat)
    assert strat.entry.kind.value == "REGULAR" and strat.entry.filled_qty == 5
    assert report["open_positions"] == [] and report["trades"]["closed"] == 1
    assert report["pnl"]["realised"] > 0
    assert report["orders"].get("CANCELLED", 0) >= 1  # the bracket stop was cancelled before exiting


async def test_bot_managed_bracket_stop(tmp_path, store):
    cfg = make_cfg(tmp_path, execution={"exit_mode": "bot_managed"})
    strat = Bracketed()
    frames = frames_at(ticks("09:16:00", "09:17:00", 1450.0) + ticks("09:17:01", "09:18:00", 1439.0))
    report = await run_replay(cfg, frames, instruments=store, strategy=strat)
    assert report["open_positions"] == [] and report["pnl"]["realised"] < 0


class Crashy(BuyOnce):
    name = "crashy"

    def on_order(self, ctx, ev):
        if ev.kind == "fill":
            raise RuntimeError("bug in strategy")


async def test_strategy_exception_disables_strategy_but_risk_and_squareoff_continue(tmp_path, store):
    cfg = make_cfg(tmp_path)
    strat = Crashy()
    frames = synth_frames(["RELIANCE"], DAY, minutes=375, step_s=5.0)
    report = await run_replay(cfg, frames, instruments=store, strategy=strat)
    assert report["strategy_errors"] == 1
    assert report["open_positions"] == []


class Rejected(Strategy):
    name = "rejected"
    params = {}

    def subscriptions(self):
        return ["RELIANCE", "NIFTY"], []

    def on_start(self, ctx):
        self.o = []

    def on_tick(self, ctx, tick):
        if not self.o and ctx.now().strftime("%H:%M:%S") >= "09:16:05":
            self.o.append(ctx.buy("RELIANCE", 500))  # > max_qty
            self.o.append(ctx.buy("RELIANCE", 1, price=1300.0))  # fat finger (10% away)


async def test_risk_rejections_are_reported(tmp_path, store):
    frames = frames_at(ticks("09:16:00", "09:16:30", 1450.0))
    strat = Rejected()
    report = await run_replay(make_cfg(tmp_path), frames, instruments=store, strategy=strat)
    assert [o.reason.split(":")[0] for o in strat.o] == ["max_qty", "price_band"]
    assert report["risk_rejections"] == {"max_qty": 1, "price_band": 1}


async def test_strategy_template_loads_and_runs(tmp_path, store):
    from dhanbot.strategy.loader import load_strategy

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    strat = load_strategy("strategies/my_strategy.py:MyStrategy", {"symbol": "RELIANCE", "timeframe": "5m"}, root)
    frames = synth_frames(["RELIANCE"], DAY, minutes=375, step_s=5.0, seed=3)
    report = await run_replay(make_cfg(tmp_path), frames, instruments=store, strategy=strat)
    assert report["strategy_errors"] == 0 and report["open_positions"] == []
