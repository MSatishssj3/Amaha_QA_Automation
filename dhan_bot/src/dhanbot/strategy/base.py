"""Strategy base class - subclass this to write your own strategy.

Minimal example::

    from dhanbot.strategy import Strategy, EMA

    class MyStrategy(Strategy):
        name = "my_strategy"
        params = {"symbol": "RELIANCE", "qty": 1}          # defaults; override in [strategy.params]

        def subscriptions(self):
            # (symbols to stream, candle series to build)
            return [self.p.symbol], [(self.p.symbol, "5m")]

        def on_start(self, ctx):
            self.ema = ctx.indicator(EMA, 20, on=(self.p.symbol, "5m"))

        def on_bar(self, ctx, bar):
            if self.ema.ready and bar.close > self.ema.value and ctx.position_qty(self.p.symbol) == 0:
                ctx.buy(self.p.symbol, self.p.qty, sl=bar.close * 0.99, target=bar.close * 1.02)

Rules of the road
* Callbacks run on the event loop: never block (no ``time.sleep``, no sync HTTP). Use
  ``ctx.run_in_executor`` for heavy computation.
* ``on_tick`` is the hot path - keep it to a few microseconds. Prefer ``on_bar`` when you can.
* Orders return immediately with an ``Order`` handle; results arrive in ``on_order``.
* Risk limits, the kill switch and square-off are enforced by the engine whatever the strategy does.
* An exception inside a callback disables the strategy for the rest of the day (exits, protective
  orders and risk keep running) and sends an alert.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..dhan.packets import Tick
    from ..models import Bar, OrderEvent
    from ..positions import Position
    from .context import Context


class Strategy:
    name: str = "strategy"
    params: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        defaults = dict(type(self).params)
        given = dict(params or {})
        if defaults:
            unknown = sorted(set(given) - set(defaults))
            if unknown:
                raise ValueError(f"strategy {self.name!r}: unknown params {unknown}; known: {sorted(defaults)}")
        defaults.update(given)
        self.p = SimpleNamespace(**defaults)

    # ---- declarations ----------------------------------------------------------------------------
    def subscriptions(self) -> tuple[list[str], list[tuple[str, str]]]:
        """Return ``(symbols, bar_series)``: symbols are config aliases (or trading symbols), bar series
        are ``(symbol, timeframe)`` pairs such as ``("NIFTY", "5m")``."""
        return [], []

    # ---- callbacks ---------------------------------------------------------------------------------
    def on_start(self, ctx: "Context") -> None:
        """Called once after preflight and history warm-up, before live data flows."""

    def on_recover(self, ctx: "Context", positions: list["Position"]) -> None:
        """Called after ``on_start`` when the bot (re)starts with open positions from earlier today."""

    def on_tick(self, ctx: "Context", tick: "Tick") -> None:
        """Every price update of a subscribed symbol (hot path)."""

    def on_bar(self, ctx: "Context", bar: "Bar") -> None:
        """A candle of a subscribed series just closed."""

    def on_order(self, ctx: "Context", ev: "OrderEvent") -> None:
        """Order lifecycle. ev.kind: ack | fill (one per execution, ev.fill set) | filled (order complete) |
        cancelled | rejected | expired | failed | unknown | modified | leg_update (super-order exit legs)."""

    def on_timer(self, ctx: "Context", name: str) -> None:
        """Fired at times registered with ``ctx.schedule("14:30", "name")``."""

    def on_stop(self, ctx: "Context") -> None:
        """Called once during shutdown."""
