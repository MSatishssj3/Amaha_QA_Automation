"""DEMO ONLY - not a trading idea. Used by ``bots demo`` on the simulated exchange.

Every ``every_min`` minutes it buys the next stock in the list with a stop-loss and a target (a Dhan
super order), then sells it ``hold_min`` minutes later if neither leg has triggered. That is enough
activity to show orders, fills, charges, the money tables and the daily budget filling up until the
budget guard starts blocking entries.
"""

from __future__ import annotations

from dhanbot.strategy import Strategy


class DemoRotation(Strategy):
    name = "demo_rotation"
    params = {"symbols": ["RELIANCE", "HDFCBANK", "INFY"], "qty": 5, "every_min": 15, "hold_min": 10,
              "stop_pct": 0.8, "target_pct": 0.8}

    def subscriptions(self):
        return list(self.p.symbols), []

    def on_start(self, ctx):
        self.i = 0
        self.next_entry = 0.0
        self.exit_at: dict[str, float] = {}

    def on_tick(self, ctx, tick):
        now = ctx.time()
        for sym, when in list(self.exit_at.items()):
            if ctx.position_qty(sym) == 0 and not ctx.open_orders(sym) and now > when - self.p.hold_min * 60 + 30:
                del self.exit_at[sym]  # closed by its stop-loss or target
            elif now >= when and ctx.position_qty(sym) > 0:
                ctx.exit(sym, reason="demo: holding time over")
                self.exit_at[sym] = now + 60
        if now < self.next_entry or not ctx.can_enter():
            return
        sym = self.p.symbols[self.i % len(self.p.symbols)]
        self.i += 1
        self.next_entry = now + self.p.every_min * 60
        px = ctx.ltp(sym)
        if px <= 0 or ctx.position_qty(sym) != 0 or ctx.open_orders(sym):
            return
        o = ctx.buy(sym, self.p.qty, sl=px * (1 - self.p.stop_pct / 100), target=px * (1 + self.p.target_pct / 100),
                    tag="demo")
        if o.status.value != "REJECTED":
            self.exit_at[sym] = now + self.p.hold_min * 60
        else:
            ctx.log.info("demo entry for %s refused: %s", sym, o.reason)
