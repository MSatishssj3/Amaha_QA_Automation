"""TEMPLATE - copy this file, rename the class, put your rules in it.

Enable it in config.toml:

    [strategy]
    name = "strategies/my_strategy.py:MyStrategy"

    [strategy.params]
    symbol = "RELIANCE"
    qty = 1

Then test it without risking money:

    dhanbot replay --synthetic          # synthetic day, instant
    dhanbot simulate --speed 60         # whole bot against a local fake Dhan
    dhanbot run                         # paper mode on the real market feed
    dhanbot replay data/ticks/<day>.bin # backtest on a day you recorded in paper/live mode

Everything below is optional - delete what you don't need.
"""

from __future__ import annotations

from dhanbot.strategy import ATR, EMA, RSI, VWAP, Strategy


class MyStrategy(Strategy):
    name = "my_strategy"
    # Defaults. Every key here can be overridden in [strategy.params]; unknown keys are rejected.
    params = {
        "symbol": "RELIANCE",  # an alias from [[instruments]] in config.toml
        "timeframe": "5m",
        "qty": 1,
        "stop_pct": 0.5,
        "target_pct": 1.0,
    }

    # 1) What data do you need? (symbols to stream, candle series to build)
    def subscriptions(self):
        return [self.p.symbol], [(self.p.symbol, self.p.timeframe)]

    # 2) Set up indicators / state. History is already loaded, so indicators are warm at 09:15.
    def on_start(self, ctx):
        on = (self.p.symbol, self.p.timeframe)
        self.ema = ctx.indicator(EMA, 20, on=on)
        self.rsi = ctx.indicator(RSI, 14, on=on)
        self.atr = ctx.indicator(ATR, 14, on=on)  # fed (high, low, close) automatically
        self.vwap = ctx.indicator(VWAP, on=on)  # fed (typical price, volume) automatically
        ctx.schedule("14:45", "late")  # -> on_timer(ctx, "late")
        # ctx.state survives same-day restarts (must be JSON-serialisable)
        ctx.state.setdefault("entries_today", 0)

    # 3) If the bot restarts mid-day with an open position, you are told here.
    def on_recover(self, ctx, positions):
        ctx.log.info("recovered positions: %s", [p.to_dict() for p in positions])

    # 4) Your rules. Prefer on_bar (candle close); on_tick runs on every price update.
    def on_bar(self, ctx, bar):
        if not (self.ema.ready and self.rsi.ready):
            return
        sym = self.p.symbol
        in_position = ctx.position_qty(sym) != 0

        if not in_position and ctx.can_enter() and not ctx.open_orders(sym):
            if bar.close > self.ema.value and self.rsi.value < 70:  # <- replace with YOUR entry rule
                px = bar.close
                ctx.buy(sym, self.p.qty,
                        sl=px * (1 - self.p.stop_pct / 100),  # stop-loss price
                        target=px * (1 + self.p.target_pct / 100))  # target price
                ctx.state["entries_today"] += 1

        elif in_position and bar.close < self.ema.value:  # <- replace with YOUR exit rule
            ctx.exit(sym, reason="close below EMA")

    def on_tick(self, ctx, tick):
        pass  # hot path: keep it tiny (or delete this method entirely - then it is never called)

    # 5) Order lifecycle: ack | fill | filled | cancelled | rejected | expired | failed | modified | leg_update
    def on_order(self, ctx, ev):
        if ev.kind == "rejected":
            ctx.log.warning("order rejected: %s", ev.order.reason)

    def on_timer(self, ctx, name):
        if name == "late" and ctx.position_qty(self.p.symbol):
            ctx.exit(self.p.symbol, reason="no positions into the close")

    def on_stop(self, ctx):
        ctx.log.info("done for the day: %s", ctx.pnl())


# ------------------------------------------------------------------------------------------------
# Options example (index options): pick the ATM call at runtime and trade it.
#
#   [[instruments]]                 # the underlying index, for its live price
#   alias = "NIFTY"
#   segment = "IDX_I"
#   security_id = 13
#   mode = "ticker"
#   [instrument_master]
#   extra_segments = ["NSE_FNO"]    # load option contracts
#
#   def on_start(self, ctx):
#       self.opt = None
#   def on_bar(self, ctx, bar):     # with ("NIFTY", "5m") in subscriptions()
#       if self.opt is None and <your signal>:
#           inst = ctx.option("NIFTY", "CE", expiry="nearest", strike="ATM")
#           self.opt = ctx.subscribe(inst, mode="full", max_qty=inst.lot_size)   # stream + allow trading
#       if self.opt and ctx.quote(self.opt) and ctx.quote(self.opt).ask:
#           ctx.buy(self.opt, ctx.instrument(self.opt).lot_size, sl=..., target=...)
# ------------------------------------------------------------------------------------------------
