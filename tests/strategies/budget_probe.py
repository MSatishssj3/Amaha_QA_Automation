"""Test strategy: whenever flat, try to buy; exit every filled entry; record every outcome.

Used by the execution / budget tests to see exactly which entries the budget guard lets through.
"""

from __future__ import annotations

from dhanbot.models import Intent, OrderStatus
from dhanbot.strategy import Strategy


class BudgetProbe(Strategy):
    name = "budget_probe"
    params = {"symbol": "RELIANCE", "qty": 2, "retry_s": 20.0}

    def subscriptions(self):
        return [self.p.symbol], []

    def on_start(self, ctx):
        self.accepted = 0  # entries that passed every check and were sent
        self.denied: list[str] = []  # reasons of entries refused before sending
        self.entry_fills = 0
        self.exit_fills = 0
        self.next_try = 0.0

    def on_tick(self, ctx, tick):
        sym = self.p.symbol
        if ctx.open_orders(sym):
            return
        if ctx.position_qty(sym) > 0:
            ctx.exit(sym, reason="probe exit")
            return
        if ctx.time() < self.next_try or not ctx.can_enter() or not ctx.quote(sym) or not ctx.quote(sym).ask:
            return
        o = ctx.buy(sym, self.p.qty)
        if o.status is OrderStatus.REJECTED:
            self.denied.append(o.reason)
            self.next_try = ctx.time() + self.p.retry_s
        else:
            self.accepted += 1

    def on_order(self, ctx, ev):
        if ev.kind == "fill" and ev.fill is not None:
            if ev.order.intent is Intent.ENTRY:
                self.entry_fills += 1
            else:
                self.exit_fills += 1
