"""The API a strategy uses to see the market and trade (``ctx`` in every callback)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Callable

from ..bars import BarSeries
from ..market_state import InstrumentState
from ..models import Instrument, Order, Side
from ..positions import Position

if TYPE_CHECKING:  # pragma: no cover
    from ..engine import Engine


class Context:
    def __init__(self, engine: "Engine") -> None:
        self._e = engine
        self.state: dict[str, Any] = {}  # persisted across same-day restarts (data/state/<strategy>.json)
        self.log = logging.getLogger(f"strategy.{engine.strategy.name if engine.strategy else 'none'}")

    # ---- orders ---------------------------------------------------------------------------------
    def buy(self, symbol: str, qty: int, price: float | None = None, *, order_type: str | None = None,
            product: str | None = None, trigger: float | None = None, sl: float | None = None,
            target: float | None = None, trail: float | None = None, validity: str | None = None,
            tag: str = "") -> Order:
        """Buy ``qty`` units. ``price=None`` -> marketable limit (best ask + buffer, capped). ``sl`` /
        ``target`` / ``trail`` are absolute prices (trail = trailing step in rupees) and become a Dhan
        super order or bot-managed bracket depending on ``execution.exit_mode``."""
        return self._e.place(Side.BUY, symbol, qty, price, order_type=order_type, product=product, trigger=trigger,
                             sl=sl, target=target, trail=trail, validity=validity, tag=tag)

    def sell(self, symbol: str, qty: int, price: float | None = None, *, order_type: str | None = None,
             product: str | None = None, trigger: float | None = None, sl: float | None = None,
             target: float | None = None, trail: float | None = None, validity: str | None = None,
             tag: str = "") -> Order:
        """Sell ``qty`` units (closes a long, or opens a short if no long is held)."""
        return self._e.place(Side.SELL, symbol, qty, price, order_type=order_type, product=product, trigger=trigger,
                             sl=sl, target=target, trail=trail, validity=validity, tag=tag)

    def exit(self, symbol: str, reason: str = "strategy exit") -> asyncio.Task:
        """Flatten the position in ``symbol``: cancels its entries / protective legs first, then exits."""
        return self._e.spawn(self._e.exit_position(self._e.resolve(symbol), role="exit", reason=reason))

    def exit_all(self, reason: str = "strategy exit_all") -> asyncio.Task:
        return self._e.spawn(self._e.flatten_all(role="exit", reason=reason))

    def cancel(self, order: Order) -> asyncio.Future:
        return self._e.oms.cancel(order)

    def modify(self, order: Order, *, price: float | None = None, qty: int | None = None,
               trigger: float | None = None) -> asyncio.Future:
        inst = order.instrument
        return self._e.oms.modify(order, qty=qty,
                                  price_paise=inst.round_to_tick(price * 100) if price is not None else None,
                                  trigger_paise=inst.round_to_tick(trigger * 100) if trigger is not None else None)

    # ---- positions / P&L -------------------------------------------------------------------------
    def position(self, symbol: str, product: str | None = None) -> Position | None:
        inst = self._e.resolve(symbol)
        for p in self._e.positions.positions.values():
            if p.inst.key == inst.key and (product is None or p.product.value == product):
                return p
        return None

    def position_qty(self, symbol: str) -> int:
        return self._e.positions.qty(self._e.resolve(symbol))

    def positions(self) -> list[Position]:
        return self._e.positions.open_positions()

    def pnl(self) -> dict[str, float]:
        b = self._e.positions
        return {"realised": round(b.realised_paise() / 100, 2), "unrealised": round(b.unrealised_paise() / 100, 2),
                "charges": round(b.charges_paise() / 100, 2), "net": round(b.net_paise() / 100, 2)}

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return self._e.oms.working(self._e.resolve(symbol) if symbol else None)

    def can_enter(self) -> bool:
        """True if a new entry would currently pass the kill switch and the entry time window."""
        return self._e.ks.can_enter and self._e.session.in_entry_window(self._e.clock.time())

    # ---- market data ----------------------------------------------------------------------------
    def instrument(self, symbol: str) -> Instrument:
        return self._e.resolve(symbol)

    def quote(self, symbol: str) -> InstrumentState | None:
        """Latest state: ltp, bid, ask, bids/asks depth (full mode), volume, oi, ohlc."""
        return self._e.market.of(self._e.resolve(symbol))

    def ltp(self, symbol: str) -> float:
        st = self.quote(symbol)
        return st.ltp if st else 0.0

    def bars(self, symbol: str, timeframe: str) -> BarSeries:
        s = self._e.bars.get(self._e.resolve(symbol).key, timeframe)
        if s is None:
            raise KeyError(f"no bar series {symbol} {timeframe}: declare it in subscriptions()")
        return s

    def indicator(self, cls: type, *args: Any, on: tuple[str, str], field: str | None = None, **kw: Any) -> Any:
        """Create an indicator fed by a bar series (warmed with history already loaded).
        ``field``: close (default) | open | high | low | hl2 | hlc3 | hlc (ATR) | pv (VWAP)."""
        ind = cls(*args, **kw)
        if field is None:
            field = {"ATR": "hlc", "VWAP": "pv"}.get(cls.__name__, "close")
        return self.bars(*on).attach(ind, field)

    def option(self, underlying: str, kind: str, *, expiry: str | date = "nearest", strike: str | float = "ATM",
               spot_symbol: str | None = None) -> Instrument:
        """Resolve an option contract using the live spot price of ``spot_symbol`` (default: underlying)."""
        spot = self.ltp(spot_symbol or underlying)
        return self._e.instruments.option(underlying, kind, spot, expiry=expiry, strike=strike,
                                          today=self.now().date())

    def subscribe(self, instrument: Instrument, mode: str = "quote", max_qty: int | None = None,
                  tradable: bool = True, bars: list[str] | None = None) -> str:
        """Start streaming (and optionally trading) an instrument at runtime, e.g. an option chosen by
        ``ctx.option``. Returns the symbol to use with buy/sell."""
        return self._e.add_instrument(instrument, mode=mode, max_qty=max_qty, tradable=tradable, bars=bars)

    async def history(self, symbol: str, timeframe: str = "1m", days: int = 5) -> list:
        return await self._e.fetch_history(self._e.resolve(symbol), timeframe, days)

    # ---- utilities ----------------------------------------------------------------------------------
    def schedule(self, hhmm: str, name: str) -> bool:
        return self._e.schedule_strategy_timer(hhmm, name)

    def notify(self, text: str) -> None:
        self._e.alert(f"[{self._e.strategy.name if self._e.strategy else '-'}] {text}", level="INFO")

    def now(self) -> datetime:
        return self._e.clock.now()

    def time(self) -> float:
        return self._e.clock.time()

    def run_in_executor(self, fn: Callable, *args: Any) -> asyncio.Future:
        return asyncio.get_running_loop().run_in_executor(None, fn, *args)

    @property
    def mode(self) -> str:
        return self._e.mode
