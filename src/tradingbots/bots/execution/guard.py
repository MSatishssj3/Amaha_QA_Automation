"""Daily budget gatekeeper: the enforcement half, which runs inside the trading engine's process.

``BudgetGuard.check`` is registered as a ``dhanbot`` risk plug-in (``Engine(extra_risk_checks=...)``),
so it runs on every ENTRY order after the engine's own rules. It is an in-memory check that costs
microseconds and makes no bus round-trip. The budget bot sends the limits (``budget.policy``) and
the account-wide usage from trades you place yourself (``budget.external``) ahead of time.

* An accepted entry **reserves** its value. Fills of the entry leg turn the reservation into used
  budget, and a cancel or reject releases the unfilled part. Two entries arriving together can
  therefore never jointly exceed a limit.
* Exits are never checked, because dhanbot never consults plug-ins for exits. Closing a position is
  always allowed.
* No policy, or a stale one with ``fail_mode = "closed"``, blocks entries. A missing or crashed
  budget bot can never silently remove your limits.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from dhanbot.clock import RealClock, epoch_at, ist_datetime
from dhanbot.models import Intent, Order, OrderEvent
from dhanbot.risk import Decision

MAIN_LEG = "MAIN"  # dhanbot's leg key for an order's own (entry-direction) fills


class BudgetGuard:
    """One guard per trading day, kept across engine restarts (``bind`` attaches it to an engine)."""

    def __init__(self, platform: str, clock: RealClock, *, exposure_fn: Callable[[], float] | None = None,
                 pnl_fn: Callable[[], float] | None = None, price_fn: Callable[[Order], float] | None = None) -> None:
        self.platform = platform
        self.clock = clock
        self.exposure_fn = exposure_fn or (lambda: 0.0)  # rupees of open exposure (positions at market)
        self.pnl_fn = pnl_fn or (lambda: 0.0)  # today's net P&L in rupees
        self.price_fn = price_fn or (lambda o: float(o.price_paise))  # reference price of an order, in paise
        self.policy: dict | None = None
        self.external: dict = {}
        self.denials: dict[str, int] = {}
        self._reset(ist_datetime(clock.time()).date())

    def bind(self, platform: str, *, exposure_fn: Callable[[], float], pnl_fn: Callable[[], float],
             price_fn: Callable[[Order], float]) -> None:
        """Attach to a (new) engine. Reservations belong to the previous engine's orders and are dropped."""
        self.platform, self.exposure_fn, self.pnl_fn, self.price_fn = platform, exposure_fn, pnl_fn, price_fn
        self.reserved.clear()

    def seed(self, usage: dict) -> None:
        """After a process restart: resume today's own usage as counted by the budget bot from fills."""
        self._roll()
        usage = usage or {}
        bot = usage.get("bot") or {}
        if usage.get("date") != self.day.isoformat() or self.entries or self.turnover:
            return
        self.turnover = float(bot.get("turnover_inr") or 0)
        self.turnover_by_stock = {k: float(v) for k, v in (bot.get("per_stock") or {}).items()}
        self.entries = int(bot.get("entries") or 0)
        self.entries_by_stock = {k: int(v) for k, v in (bot.get("entries_by_stock") or {}).items()}

    def _reset(self, d: date) -> None:
        self.day = d
        self._day_end = epoch_at(d + timedelta(days=1), "00:00")
        self.turnover = 0.0
        self.turnover_by_stock: dict[str, float] = {}
        self.entries = 0
        self.entries_by_stock: dict[str, int] = {}
        self.reserved: dict[int, tuple[str, float]] = {}  # order local_id -> (symbol, rupees not yet filled)
        self.denials = {}
        if self.external.get("date") != d.isoformat():
            self.external = {}

    def _roll(self) -> None:
        if self.clock.time() >= self._day_end:
            self._reset(ist_datetime(self.clock.time()).date())

    # ---- inputs (from the budget bot) ----------------------------------------------------------------
    def update_policy(self, msg: dict) -> None:
        if self.policy is not None and float(msg.get("ts") or 0) < float(self.policy.get("ts") or 0):
            return  # an older message (e.g. read from the key-value store after a newer event)
        self.policy = msg

    def update_external(self, msg: dict) -> None:
        self._roll()
        if msg.get("date") == self.day.isoformat():
            self.external = msg

    @property
    def limits(self) -> dict:
        return (self.policy or {}).get("limits") or {}

    def policy_problem(self) -> str:
        """Why entries are blocked regardless of the limits ('' when the policy is usable)."""
        if self.policy is None:
            return "no daily budget received from the budget bot yet"
        L = self.limits
        if L.get("fail_mode", "closed") == "closed":
            age = self.clock.time() - float(self.policy.get("ts") or 0)
            max_age = float(L.get("policy_max_age_s") or 600)
            if age > max_age:
                return f"budget bot silent for {age / 60:.0f} min (fail_mode = closed)"
        return ""

    def _value(self, order: Order) -> float:
        return order.qty * self.price_fn(order) * order.instrument.multiplier / 100.0

    def _deny(self, rule: str, text: str, record: bool) -> Decision:
        if record:
            self.denials[rule] = self.denials.get(rule, 0) + 1
        return Decision(False, f"{rule}: {text}")

    # ---- the check (hot path) ----------------------------------------------------------------------------
    def check(self, order: Order, record: bool) -> Decision | None:
        self._roll()
        problem = self.policy_problem()
        if problem:
            return self._deny("budget_policy", f"{problem} - entries blocked", record)
        L = self.limits
        if not L.get("enabled", True):
            return None
        sym = order.instrument.trading_symbol
        loss_lim = float(L.get("max_daily_loss_inr") or 0)
        if loss_lim:
            pnl = self.pnl_fn()
            if pnl <= -loss_lim:
                return self._deny("budget_daily_loss", f"today's P&L ₹{pnl:,.0f} reached the limit -₹{loss_lim:,.0f}",
                                  record)
        ext = self.external if L.get("count_manual_trades", True) else {}
        ext_trades = int(ext.get("trades") or 0)
        max_trades = int(L.get("max_trades_per_day") or 0)
        if max_trades and self.entries + ext_trades >= max_trades:
            return self._deny("budget_trades", f"{self.entries + ext_trades} entries today (limit {max_trades})", record)
        max_ts = int(L.get("max_trades_per_stock") or 0)
        n_stock = self.entries_by_stock.get(sym, 0) + int((ext.get("trades_by_stock") or {}).get(sym) or 0)
        if max_ts and n_stock >= max_ts:
            return self._deny("budget_trades_stock", f"{sym}: {n_stock} entries today (limit {max_ts})", record)
        value = self._value(order)
        res_total = res_stock = 0.0
        for s, v in self.reserved.values():
            res_total += v
            if s == sym:
                res_stock += v
        used = self.turnover + res_total + float(ext.get("turnover_inr") or 0)
        lim = float(L.get("daily_capital_limit_inr") or 0)
        if lim and used + value > lim + 1e-6:
            return self._deny("budget_daily_capital", f"₹{used + value:,.0f} would exceed today's limit ₹{lim:,.0f} "
                                                      f"(used ₹{used:,.0f}, this order ₹{value:,.0f})", record)
        overrides = L.get("per_stock_overrides") or {}
        stock_lim = float(overrides[sym] if sym in overrides else (L.get("per_stock_daily_limit_inr") or 0))
        used_s = self.turnover_by_stock.get(sym, 0.0) + res_stock + float((ext.get("per_stock") or {}).get(sym) or 0)
        if stock_lim and used_s + value > stock_lim + 1e-6:
            return self._deny("budget_per_stock", f"{sym}: ₹{used_s + value:,.0f} would exceed ₹{stock_lim:,.0f} today",
                              record)
        plat_lim = float((L.get("per_platform_daily_limit_inr") or {}).get(self.platform) or 0)
        if plat_lim and used + value > plat_lim + 1e-6:
            return self._deny("budget_platform", f"{self.platform}: ₹{used + value:,.0f} would exceed ₹{plat_lim:,.0f} "
                                                 "today", record)
        in_use_lim = float(L.get("max_capital_in_use_inr") or 0)
        if in_use_lim:
            in_use = self.exposure_fn() + res_total
            if in_use + value > in_use_lim + 1e-6:
                return self._deny("budget_capital_in_use", f"₹{in_use + value:,.0f} in use would exceed "
                                                           f"₹{in_use_lim:,.0f}", record)
        if record:
            self.reserved[order.local_id] = (sym, value)
            self.entries += 1
            self.entries_by_stock[sym] = self.entries_by_stock.get(sym, 0) + 1
        return None

    # ---- feedback from the order manager ------------------------------------------------------------------
    def on_event(self, ev: OrderEvent) -> None:
        o = ev.order
        if o.intent is not Intent.ENTRY or o.local_id not in self.reserved:
            return
        sym, left = self.reserved[o.local_id]
        f = ev.fill
        if ev.kind == "fill" and f is not None and f.leg == MAIN_LEG:  # super-order exit legs are not spending
            v = f.qty * f.price_paise * o.instrument.multiplier / 100.0
            self.turnover += v
            self.turnover_by_stock[sym] = self.turnover_by_stock.get(sym, 0.0) + v
            left = max(0.0, left - v)
            self.reserved[o.local_id] = (sym, left)
        if o.is_done:
            del self.reserved[o.local_id]  # whatever did not fill is released
            if o.filled_qty == 0:  # never traded: give the entry back
                self.entries = max(0, self.entries - 1)
                self.entries_by_stock[sym] = max(0, self.entries_by_stock.get(sym, 1) - 1)

    def usage(self) -> dict:
        self._roll()
        return {"date": self.day.isoformat(), "turnover_inr": round(self.turnover, 2),
                "reserved_inr": round(sum(v for _, v in self.reserved.values()), 2), "entries": self.entries,
                "per_stock": {k: round(v, 2) for k, v in self.turnover_by_stock.items()},
                "entries_by_stock": dict(self.entries_by_stock), "denials": dict(self.denials),
                "external": {k: self.external.get(k) for k in ("turnover_inr", "trades") if k in self.external},
                "policy_version": (self.policy or {}).get("version"), "policy_problem": self.policy_problem()}
