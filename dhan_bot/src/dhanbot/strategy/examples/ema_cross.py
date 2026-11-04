"""EXAMPLE ONLY - demonstrates the strategy API. Not a trading recommendation; replace it with your own.

Long-only EMA crossover on closed candles:
* fast EMA crosses above slow EMA and we are flat -> buy with a stop-loss and target
  (sent as one Dhan super order, so the stop lives at the broker even if the bot dies);
* fast crosses below slow while long -> exit;
* the engine squares off anything left at the configured square-off time.
"""

from __future__ import annotations

from dhanbot.strategy import EMA, Strategy


class EmaCross(Strategy):
    name = "ema_cross"
    params = {
        "symbol": "RELIANCE",
        "timeframe": "1m",
        "fast": 9,
        "slow": 21,
        "qty": 1,
        "stop_pct": 0.5,
        "target_pct": 1.0,
    }

    def subscriptions(self):
        return [self.p.symbol], [(self.p.symbol, self.p.timeframe)]

    def on_start(self, ctx):
        on = (self.p.symbol, self.p.timeframe)
        self.fast = ctx.indicator(EMA, self.p.fast, on=on)
        self.slow = ctx.indicator(EMA, self.p.slow, on=on)
        self.prev_diff = (self.fast.value - self.slow.value) if (self.fast.ready and self.slow.ready) else None
        ctx.log.info("ema_cross ready (history bars: %d)", len(ctx.bars(*on)))

    def on_bar(self, ctx, bar):
        if not (self.fast.ready and self.slow.ready):
            return
        diff = self.fast.value - self.slow.value
        prev, self.prev_diff = self.prev_diff, diff
        if prev is None:
            return
        sym = self.p.symbol
        pos = ctx.position_qty(sym)
        if prev <= 0 < diff and pos == 0 and ctx.can_enter() and not ctx.open_orders(sym):
            px = bar.close
            ctx.buy(sym, self.p.qty, sl=px * (1 - self.p.stop_pct / 100), target=px * (1 + self.p.target_pct / 100),
                    tag="ema_up")
        elif prev >= 0 > diff and pos > 0:
            ctx.exit(sym, reason="ema cross down")

    def on_order(self, ctx, ev):
        if ev.kind in ("fill", "rejected"):
            ctx.log.info("%s %s", ev.kind, ev.order)
