"""Daily budget gatekeeper: BudgetGuard rules (unit) and the real dhan_bot engine inside the team.

The team tests run the execution bot's engine against the simulated exchange, together with the
config, budget and (where needed) market-data bots on one bus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dhanbot.clock import VirtualClock, epoch_at, ist_datetime
from dhanbot.dhan.endpoints import Endpoints
from dhanbot.dhan.rest import DhanRest
from dhanbot.models import Fill, Instrument, Order, OrderEvent, OrderStatus, Product, Side

from tradingbots.bots.execution.guard import BudgetGuard
from tradingbots.core import topics

from .helpers import make_settings, read_all, wait_for
from .team import Team, next_weekday_at

REL = Instrument(2885, "NSE_EQ", 1, "RELIANCE", tick_paise=10)
INFY = Instrument(1594, "NSE_EQ", 1, "INFY", tick_paise=10)
PROBE = Path(__file__).parent / "strategies" / "budget_probe.py"


# ---- BudgetGuard unit tests ----------------------------------------------------------------------------------
def limits(**kw) -> dict:
    base = {"enabled": True, "daily_capital_limit_inr": 10_000.0, "max_capital_in_use_inr": 0.0,
            "per_stock_daily_limit_inr": 0.0, "per_stock_overrides": {}, "per_platform_daily_limit_inr": {},
            "max_trades_per_day": 0, "max_trades_per_stock": 0, "max_daily_loss_inr": 0.0, "alert_at_pct": [80, 100],
            "count_manual_trades": True, "fail_mode": "closed", "policy_max_age_s": 600.0}
    base.update(kw)
    return base


class Env:
    def __init__(self, **kw) -> None:
        self.clock = VirtualClock(epoch_at(ist_datetime(next_weekday_at("10:00")).date(), "10:00"))
        self.exposure = 0.0
        self.pnl = 0.0
        self.g = BudgetGuard("dhan", self.clock, exposure_fn=lambda: self.exposure, pnl_fn=lambda: self.pnl,
                             price_fn=lambda o: float(o.price_paise))
        self.n = 0
        if kw.get("policy", True):
            self.g.update_policy({"version": 1, "ts": self.clock.time(), "limits": limits(**kw.get("limits", {}))})

    def order(self, qty: int = 2, price: float = 1000.0, inst: Instrument = REL) -> Order:
        self.n += 1
        return Order(self.n, inst, Side.BUY, qty, price_paise=int(price * 100))

    def check(self, o: Order, record: bool = True):
        d = self.g.check(o, record)
        return "ok" if d is None else d.reason

    def fill(self, o: Order, qty: int, price: float | None = None, leg: str = "MAIN", done: bool = False) -> None:
        px = (price or o.price_paise / 100) * 100
        o.filled_qty += qty if leg == "MAIN" else 0
        o.status = OrderStatus.FILLED if done else OrderStatus.PART_FILLED
        side = o.side if leg == "MAIN" else o.side.opposite
        self.g.on_event(OrderEvent("fill", o, Fill(o.local_id, o.instrument, Product.INTRADAY, side, qty, px, leg,
                                                    self.clock.time())))

    def finish(self, o: Order, status: OrderStatus = OrderStatus.CANCELLED) -> None:
        o.status = status
        self.g.on_event(OrderEvent(status.value.lower(), o))


def test_guard_fails_closed_without_or_with_a_stale_policy():
    e = Env(policy=False)
    assert e.check(e.order()).startswith("budget_policy: no daily budget")
    assert e.check(e.order(), record=False).startswith("budget_policy") and e.g.denials == {"budget_policy": 1}
    e.g.update_policy({"version": 1, "ts": e.clock.time(), "limits": limits()})
    assert e.check(e.order()) == "ok"
    e.clock.advance_to(e.clock.time() + 601)  # budget bot silent for 10 minutes
    assert "silent" in e.check(e.order())
    e.g.update_policy({"version": 1, "ts": e.clock.time(), "limits": limits(fail_mode="last_known")})
    e.clock.advance_to(e.clock.time() + 7200)
    assert e.check(e.order()) == "ok"  # last_known keeps enforcing the old policy
    older = {"version": 1, "ts": e.g.policy["ts"] - 1, "limits": limits(daily_capital_limit_inr=1.0)}
    e.g.update_policy(older)
    assert e.g.limits["daily_capital_limit_inr"] == 10_000.0  # an older message never replaces a newer one


def test_guard_reserves_converts_and_releases():
    e = Env()
    a = e.order(qty=4)  # 4,000
    b = e.order(qty=4)  # 4,000 -> 8,000 reserved
    assert e.check(a) == "ok" and e.check(b) == "ok"
    c = e.order(qty=3)  # would make 11,000 > 10,000 even though nothing has filled yet
    assert e.check(c).startswith("budget_daily_capital")
    assert e.check(e.order(qty=3), record=False).startswith("budget_daily_capital")
    assert e.g.denials["budget_daily_capital"] == 1  # dry runs are not counted
    e.fill(a, 4, 1000.0, done=True)  # converted: turnover 4,000, reservation released
    e.fill(b, 1, 1010.0)  # partial fill, then cancelled -> 1,010 used, 2,990 released
    e.finish(b)
    u = e.g.usage()
    assert u["turnover_inr"] == pytest.approx(5010.0) and u["reserved_inr"] == 0 and u["entries"] == 2
    assert e.check(e.order(qty=4)) == "ok"  # 5,010 + 4,000 <= 10,000
    d = e.order(qty=1)
    assert e.check(d).startswith("budget_daily_capital")  # 9,010 + 1,000 > 10,000
    never = e.g.reserved.copy()
    assert len(never) == 1
    (lid,) = never
    o = Order(lid, REL, Side.BUY, 4, price_paise=100000)
    e.finish(o, OrderStatus.REJECTED)  # broker rejected it: nothing used, entry given back
    assert e.g.usage()["entries"] == 2 and e.g.usage()["reserved_inr"] == 0


def test_guard_super_order_exit_legs_are_not_spending():
    e = Env()
    o = e.order(qty=5)
    assert e.check(o) == "ok"
    e.fill(o, 5, 1000.0, done=True)
    e.fill(o, 5, 1010.0, leg="TARGET_LEG")  # the target leg sells: an exit
    assert e.g.usage()["turnover_inr"] == pytest.approx(5000.0)


def test_guard_limits_per_stock_platform_exposure_counts_loss_and_external():
    e = Env(limits={"per_stock_daily_limit_inr": 3000, "per_stock_overrides": {"INFY": 1500},
                    "per_platform_daily_limit_inr": {"dhan": 9000, "zerodha": 1}, "max_capital_in_use_inr": 6000,
                    "max_trades_per_day": 4, "max_trades_per_stock": 2, "max_daily_loss_inr": 500})
    assert e.check(e.order(qty=2, inst=INFY)).startswith("budget_per_stock: INFY")  # override 1,500 < 2,000
    assert e.check(e.order(qty=1, inst=INFY)) == "ok"
    assert e.check(e.order(qty=3)) == "ok"  # RELIANCE 3,000 (per-stock limit exactly)
    assert e.check(e.order(qty=1)).startswith("budget_per_stock: RELIANCE")
    e.exposure = 5500.0
    assert e.check(e.order(qty=1, price=100, inst=INFY)).startswith("budget_capital_in_use")
    e.exposure = 0.0
    e.g.reserved.clear()
    assert e.check(e.order(qty=1, price=100, inst=INFY)) == "ok"  # third entry
    assert e.check(e.order(qty=1, price=100, inst=INFY)).startswith("budget_trades_stock: INFY")
    e.g.update_external({"date": "1999-01-01", "trades": 50})  # another day: ignored
    assert e.g.external == {}
    e.g.update_external({"date": e.g.day.isoformat(), "turnover_inr": 4000.0, "trades": 1,
                         "per_stock": {"HDFCBANK": 4000.0}, "trades_by_stock": {"HDFCBANK": 1}})
    hd = Instrument(1333, "NSE_EQ", 1, "HDFCBANK", tick_paise=10)
    assert e.check(e.order(qty=1, price=100, inst=hd)).startswith("budget_trades: 4 entries")  # 3 own + 1 manual
    e.g.entries = 0
    assert e.check(e.order(qty=1, price=100, inst=hd)).startswith("budget_per_stock: HDFCBANK")  # manual 4,000 > 3,000
    e.g.external = {}
    e.g.turnover = 8950.0
    assert e.check(e.order(qty=1, price=100, inst=hd)).startswith("budget_platform: dhan")  # 9,050 > 9,000 on dhan
    e.g.turnover = 0.0
    e.pnl = -500.0
    assert e.check(e.order(qty=1, price=100, inst=hd)).startswith("budget_daily_loss")
    e.g.update_policy({"version": 2, "ts": e.clock.time(), "limits": limits(enabled=False)})
    assert e.check(e.order(qty=99)) == "ok"  # budget switched off


def test_guard_new_day_resets_and_seed_resumes():
    e = Env()
    o = e.order(qty=9)
    assert e.check(o) == "ok"
    e.fill(o, 9, 1000.0, done=True)
    assert e.check(e.order(qty=2)).startswith("budget_daily_capital")
    e.clock.advance_to(epoch_at(e.g.day, "00:00") + 86400 + 9 * 3600)
    e.g.update_policy({"version": 1, "ts": e.clock.time(), "limits": limits()})
    assert e.check(e.order(qty=2)) == "ok" and e.g.usage()["turnover_inr"] == 0
    g2 = BudgetGuard("dhan", e.clock)
    g2.seed({"date": g2.day.isoformat(), "bot": {"turnover_inr": 9500.0, "entries": 3, "per_stock": {"RELIANCE": 9500},
                                                 "entries_by_stock": {"RELIANCE": 3}}})
    g2.update_policy({"version": 1, "ts": e.clock.time(), "limits": limits()})
    d = g2.check(Order(1, REL, Side.BUY, 1, price_paise=100000), True)
    assert d is not None and d.reason.startswith("budget_daily_capital")  # 9,500 resumed + 1,000 > 10,000


# ---- the real engine inside the team ----------------------------------------------------------------------------
def write_dhanbot_config(tmp: Path, qty: int = 2) -> Path:
    p = tmp / "config" / "dhanbot.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"""
[mode]
mode = "paper"
[session]
profile = "nse"
[feed]
record_ticks = false
[risk]
max_daily_loss_inr = 5000
max_consecutive_losses = 100
max_orders_per_day = 1000
[perf]
gc_freeze = false
[paths]
data_dir = "../data/execution/dhan"
[[instruments]]
alias = "RELIANCE"
segment = "NSE_EQ"
trading_symbol = "RELIANCE"
mode = "full"
product = "INTRADAY"
max_qty = 50
[strategy]
name = "{PROBE.as_posix()}:BudgetProbe"
[strategy.params]
qty = {qty}
retry_s = 20.0
""", encoding="utf-8")
    return p


def team_settings(tmp: Path, *, enabled: bool = True, **budget):
    b = {"daily_capital_limit_inr": 4000.0, "per_stock_daily_limit_inr": 0.0, "max_capital_in_use_inr": 0.0,
         "max_trades_per_day": 0, "max_trades_per_stock": 0, "max_daily_loss_inr": 0.0}
    b.update(budget)
    return make_settings(tmp, platform={"name": "simulated"}, budget=b,
                         execution={"enabled": enabled, "dhanbot_config": str(write_dhanbot_config(tmp))},
                         market_data={"snapshot_interval_s": 30})


def probe(team: Team):
    """The running probe strategy once the engine has started it (on_start called), else None."""
    e = team.bots["execution"].engine
    s = e.strategy if e is not None else None
    return s if s is not None and hasattr(s, "denied") else None


async def alerts(team: Team, prefix: str) -> list[dict]:
    out = []
    for m in await read_all(team.client, topics.ALERT):
        if str(m.data.get("key", "")).startswith(prefix):
            out.append(m.data)
    return out


async def test_budget_blocks_entries_beyond_the_daily_limit_and_applies_changes_live(tmp_path):
    team = Team(tmp_path, ["sim_market", "config", "budget", "execution"], settings=team_settings(tmp_path), speed=30)
    async with team:
        c = team.client
        assert await wait_for(lambda: probe(team) is not None, 40), team.bots["execution"].note
        st = team.bots["execution"]
        assert st.exec_mode == "simulated-exchange"
        # entry 1 (2 x ~1450 = ~2900) fills and is exited; entry 2 would make ~5800 > 4000 -> blocked
        assert await wait_for(lambda: any(r.startswith("budget_daily_capital") for r in probe(team).denied), 60), \
            (probe(team).denied, st.guard.usage())
        p = probe(team)
        assert p.entry_fills >= 1 and p.accepted == 1
        assert await wait_for(lambda: probe(team).exit_fills >= 1, 30)  # exits are never blocked
        usage = await c.kv_get(topics.KV_BUDGET_USAGE)
        assert await wait_for(lambda: c.kv_get(topics.KV_BUDGET_USAGE), 5)

        async def usage_ok():
            u = await c.kv_get(topics.KV_BUDGET_USAGE) or {}
            return 2500 < (u.get("bot") or {}).get("turnover_inr", 0) < 3200 and u.get("budget_rejects", 0) >= 1

        assert await wait_for(usage_ok, 20), usage
        usage = await c.kv_get(topics.KV_BUDGET_USAGE)
        dc = next(i for i in usage["limits"] if i["rule"] == "daily_capital")
        assert dc["limit"] == 4000 and 60 < dc["pct"] < 80 and usage["bot"]["entries"] == 1
        assert await wait_for(lambda: alerts(team, "budget:reject:budget_daily_capital"), 10)
        # the dashboard's controls reach the engine
        r = await c.request(topics.cmd("execution", "control"), {"action": "pause"})
        assert r["ok"] and r["done"] and r["result"]["state"] == "PAUSED"
        r = await c.request(topics.cmd("execution", "control"), {"action": "resume"})
        assert r["result"]["state"] == "RUNNING"
        # raise the limit: the next attempt goes through, no restart
        up = await c.request(topics.cmd("config", "update"),
                             {"patch": {"budget": {"daily_capital_limit_inr": 20000}}, "author": "test"})
        assert up["applied"], up
        assert await wait_for(lambda: probe(team).entry_fills >= 2 and probe(team).accepted >= 2, 60), \
            (probe(team).denied[-3:], st.guard.usage())
        changed = await alerts(team, "budget-changed")
        assert changed and "daily_capital_limit_inr: 4000.0 -> 20000.0" in changed[-1]["text"]
        status = await c.request(topics.cmd("execution", "status"), {})
        assert status["running"] and status["guard"]["denials"].get("budget_daily_capital", 0) >= 1
        ex = await c.kv_get(topics.KV_EXEC_STATUS)
        assert ex["running"] and ex["mode"] == "simulated-exchange" and ex["cid_prefix"]


async def test_your_own_trades_shrink_the_budget(tmp_path):
    s = team_settings(tmp_path, enabled=False)
    team = Team(tmp_path, ["sim_market", "config", "market_data", "budget", "execution"], settings=s, speed=30)
    async with team:
        c = team.client
        assert await wait_for(lambda: c.kv_get(topics.KV_SIM), 10)
        sim = await c.kv_get(topics.KV_SIM)
        rest = DhanRest(Endpoints(**sim["endpoints"]), sim["client_id"], sim["token"])
        await rest.start()
        try:  # a trade you place yourself in the Dhan app: BUY 2 RELIANCE, not by the bot
            body = rest.dumps({"transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "CNC",
                               "orderType": "MARKET", "validity": "DAY", "securityId": "2885", "quantity": 2,
                               "price": 0, "correlationId": "MANUAL1"})
            assert (await rest.place_order_raw(body)).ok
        finally:
            await rest.close()

        async def external_seen():
            u = await c.kv_get(topics.KV_BUDGET_USAGE) or {}
            return (u.get("external") or {}).get("turnover_inr", 0) > 2500

        await c.request(topics.cmd("market_data", "snapshot"), {"full": False})
        assert await wait_for(external_seen, 30), await c.kv_get(topics.KV_BUDGET_USAGE)
        u = await c.kv_get(topics.KV_BUDGET_USAGE)
        assert u["external"]["trades"] == 1 and u["bot"]["turnover_inr"] == 0
        dc = next(i for i in u["limits"] if i["rule"] == "daily_capital")
        assert dc["used"] > 2500 and dc["remaining"] < 1500
        # now let the strategy trade: its first entry (~2900) no longer fits in the ~1100 that is left
        up = await c.request(topics.cmd("config", "update"), {"patch": {"execution": {"enabled": True}}})
        assert up["applied"]
        assert await wait_for(lambda: probe(team) is not None and probe(team).denied, 60), team.bots["execution"].note
        assert probe(team).denied[0].startswith("budget_daily_capital") and probe(team).accepted == 0
        assert "used ₹2," in probe(team).denied[0]  # the manual trade is what used the budget


async def test_entries_blocked_until_the_budget_bot_publishes_a_policy(tmp_path):
    team = Team(tmp_path, ["sim_market", "config", "execution"], settings=team_settings(tmp_path, daily_capital_limit_inr=50000),
                speed=30)
    async with team:
        assert await wait_for(lambda: probe(team) is not None and probe(team).denied, 60), team.bots["execution"].note
        assert probe(team).denied[0].startswith("budget_policy: no daily budget")
        assert probe(team).accepted == 0
        assert await wait_for(lambda: read_all(team.client, topics.EXEC_REJECT), 10)
        rej = [m.data for m in await read_all(team.client, topics.EXEC_REJECT)]
        assert rej and rej[0]["rule"] == "budget_policy" and rej[0]["budget"] is True
        await team.start("budget")  # the gatekeeper comes up: limits known -> entries allowed
        assert await wait_for(lambda: probe(team).accepted >= 1 and probe(team).entry_fills >= 1, 60), \
            probe(team).denied[-3:]
