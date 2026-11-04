"""Pre-trade risk checks and the kill switch. Runs identically in paper and live mode.

Every order passes ``RiskEngine.check`` *before* it reaches the OMS. Checks are pure in-memory
lookups (no I/O) so they cost microseconds on the hot path.

* ENTRY orders (open / increase a position) must pass every rule.
* EXIT orders (reduce a position) are **never blocked by limits** - only by "reduce-only": an exit
  can never flip or increase a position, counting exits that are already working.

Kill-switch levels
* PAUSED  - no new entries; exits and protective orders keep working. ``resume`` lifts it.
* HALTED  - latched for the day: the engine cancels all bot orders and flattens bot positions.
"""

from __future__ import annotations

import enum
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .clock import RealClock
from .config import AppConfig, InstrumentCfg
from .market_state import MarketState
from .models import Instrument, Intent, Order, OrderKind, OrderType, Side
from .positions import ClosedTrade, PositionBook

log = logging.getLogger(__name__)


class TradingState(str, enum.Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"


class KillSwitch:
    def __init__(self, clock: RealClock) -> None:
        self.clock = clock
        self.state = TradingState.RUNNING
        self.reason = ""
        self.since = 0.0
        self.listeners: list[Callable[[TradingState, str], None]] = []

    def _set(self, state: TradingState, reason: str) -> None:
        if state is self.state and reason == self.reason:
            return
        self.state, self.reason, self.since = state, reason, self.clock.time()
        level = logging.ERROR if state is TradingState.HALTED else logging.WARNING
        log.log(level, "TRADING %s: %s", state.value, reason or "-")
        for fn in self.listeners:
            try:
                fn(state, reason)
            except Exception:
                log.exception("kill-switch listener failed")

    def pause(self, reason: str) -> None:
        if self.state is TradingState.RUNNING:
            self._set(TradingState.PAUSED, reason)

    def halt(self, reason: str) -> None:
        if self.state is not TradingState.HALTED:
            self._set(TradingState.HALTED, reason)

    def resume(self) -> bool:
        if self.state is TradingState.HALTED:
            log.warning("cannot resume: trading is HALTED for the day (%s)", self.reason)
            return False
        if self.state is TradingState.PAUSED:
            self._set(TradingState.RUNNING, "resumed")
        return True

    @property
    def can_enter(self) -> bool:
        return self.state is TradingState.RUNNING


@dataclass(slots=True)
class Decision:
    ok: bool
    reason: str = ""


OK = Decision(True)


class RiskEngine:
    def __init__(self, cfg: AppConfig, positions: PositionBook, market: MarketState, clock: RealClock,
                 killswitch: KillSwitch, *, limits: dict[tuple[int, int], InstrumentCfg] | None = None,
                 in_entry_window: Callable[[float], bool] | None = None,
                 token_seconds_left: Callable[[], float] | None = None,
                 working_exit_qty: Callable[[Instrument], int] | None = None,
                 working_entries: Callable[[Instrument], int] | None = None) -> None:
        self.cfg = cfg
        self.r = cfg.risk
        self.positions = positions
        self.market = market
        self.clock = clock
        self.ks = killswitch
        self.limits = limits or {}
        self.in_entry_window = in_entry_window or (lambda now: True)
        self.token_seconds_left = token_seconds_left or (lambda: float("inf"))
        self.working_exit_qty = working_exit_qty or (lambda inst: 0)
        self.working_entries = working_entries or (lambda inst: 0)
        # Plug-in checks for ENTRY orders, run after every built-in rule: fn(order, record) -> Decision | None.
        # ``record`` is False for dry runs (warm-up) - a check must not reserve anything then.
        self.extra_checks: list[Callable[[Order, bool], Decision | None]] = []
        self.funds_available_inr: float | None = None
        self.broker_pnl_inr: float | None = None
        self.orders_today = 0
        self.entries_by_instrument: dict[tuple[int, int], int] = {}
        self.consecutive_losses = 0
        self.closed_trades = 0
        self.wins = 0
        self._recent: deque[tuple[float, tuple, str, int]] = deque()
        self._rejects: deque[float] = deque()
        self.rejections: dict[str, int] = {}
        self.peak_net_inr = 0.0
        self.min_net_inr = 0.0

    # ---- helpers -------------------------------------------------------------------------------
    def _deny(self, reason: str, record: bool = True) -> Decision:
        if record:
            key = reason.split(":", 1)[0]
            self.rejections[key] = self.rejections.get(key, 0) + 1
        return Decision(False, reason)

    def net_pnl_inr(self) -> float:
        if self.r.include_manual_trades and self.broker_pnl_inr is not None:
            return self.broker_pnl_inr
        return self.positions.net_paise() / 100.0

    def _margin_factor(self, inst: Instrument, order: Order) -> float | None:
        mf = self.cfg.margin_factors
        if inst.is_option:
            return mf.get("OPTION:BUY") if order.side is Side.BUY else mf.get("OPTION:SELL")
        return mf.get(f"{inst.segment}:{order.product.value}")

    def reference_price_paise(self, order: Order) -> float:
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LOSS) and order.price_paise:
            return float(order.price_paise)
        st = self.market.of(order.instrument)
        if st is not None and st.ltp > 0:
            return st.ltp * 100.0
        return float(order.trigger_paise or order.price_paise)

    # ---- the check -------------------------------------------------------------------------------
    def check(self, order: Order, record: bool = True) -> Decision:
        """``record=False`` evaluates without counting the order (used to warm up the hot path)."""
        inst = order.instrument
        if order.qty <= 0:
            return self._deny("qty: quantity must be positive", record)
        if inst.lot_size > 1 and order.qty % inst.lot_size:
            return self._deny(f"lot: quantity {order.qty} is not a multiple of lot size {inst.lot_size}", record)
        if order.intent is Intent.EXIT:
            return self._check_exit(order, record)
        return self._check_entry(order, record)

    def _check_exit(self, order: Order, record: bool = True) -> Decision:
        inst = order.instrument
        pos_qty = self.positions.qty(inst, order.product)
        if pos_qty == 0 or (pos_qty > 0) == (order.side is Side.BUY):
            return self._deny(f"reduce_only: no {('short' if order.side is Side.BUY else 'long')} position "
                              f"in {inst.trading_symbol} to exit", record)
        pending = self.working_exit_qty(inst)
        if order.qty + pending > abs(pos_qty):
            return self._deny(f"reduce_only: exit qty {order.qty} + working exits {pending} exceeds position "
                              f"{abs(pos_qty)}", record)
        band = self._band_check(order, multiplier=2.0, record=record)
        if band is not None:
            return band
        if record:
            self.orders_today += 1
        return OK

    def _band_check(self, order: Order, multiplier: float = 1.0, record: bool = True) -> Decision | None:
        if order.order_type not in (OrderType.LIMIT, OrderType.STOP_LOSS) or not order.price_paise:
            return None
        st = self.market.of(order.instrument)
        if st is None or st.ltp <= 0:
            return None
        ltp = st.ltp * 100
        band = (self.r.price_band_options_pct if order.instrument.is_option else self.r.price_band_pct) * multiplier
        dev = abs(order.price_paise - ltp) / ltp * 100
        if dev > band:
            return self._deny(f"price_band: limit {order.price_paise / 100:.2f} is {dev:.2f}% from LTP "
                              f"{st.ltp:.2f} (max {band:.1f}%)", record)
        if st.upper_circuit and order.price_paise > st.upper_circuit * 100 + 0.5:
            return self._deny(f"circuit: price above upper circuit {st.upper_circuit}", record)
        if st.lower_circuit and order.price_paise < st.lower_circuit * 100 - 0.5:
            return self._deny(f"circuit: price below lower circuit {st.lower_circuit}", record)
        return None

    def _check_entry(self, order: Order, record: bool = True) -> Decision:
        inst = order.instrument
        r = self.r
        now = self.clock.time()
        if not self.ks.can_enter:
            return self._deny(f"killswitch: trading {self.ks.state.value} ({self.ks.reason})", record)
        lim = self.limits.get(inst.key)
        if lim is None or not lim.tradable or not inst.tradable:
            return self._deny(f"whitelist: {inst.trading_symbol} is not a tradable instrument in config", record)
        if order.qty > lim.max_qty:
            return self._deny(f"max_qty: {order.qty} > max_qty {lim.max_qty} for {lim.alias}", record)
        if inst.freeze_qty and order.qty > inst.freeze_qty and not self.cfg.execution.allow_slicing:
            return self._deny(f"freeze_qty: {order.qty} exceeds exchange freeze qty {inst.freeze_qty}", record)
        if not self.in_entry_window(now):
            return self._deny("time_window: outside entry window", record)
        if self.token_seconds_left() < r.token_min_validity_s:
            return self._deny("token: access token expires too soon", record)
        age = self.market.age(inst, now)
        if age > self.cfg.feed.stale_after_s:
            return self._deny(f"stale_feed: last tick {age:.1f}s ago (max {self.cfg.feed.stale_after_s}s)", record)
        band = self._band_check(order, record=record)
        if band is not None:
            return band
        if order.kind is OrderKind.SUPER:
            px = order.price_paise or self.reference_price_paise(order)
            sign = order.side.sign
            if order.target_paise and (order.target_paise - px) * sign <= 0:
                return self._deny("bracket: target must be beyond the entry price", record)
            if order.sl_paise and (px - order.sl_paise) * sign <= 0:
                return self._deny("bracket: stop-loss must be on the losing side of the entry price", record)
        ref = self.reference_price_paise(order)
        value = order.qty * ref * inst.multiplier / 100.0
        if value > r.max_order_value_inr:
            return self._deny(f"order_value: ₹{value:,.0f} > max ₹{r.max_order_value_inr:,.0f}", record)
        existing = self.positions.qty(inst, order.product)
        if existing == 0 and len(self.positions.open_positions()) >= r.max_open_positions:
            return self._deny(f"max_open_positions: already {r.max_open_positions} open", record)
        exposure = self.positions.gross_exposure_paise() / 100.0
        if exposure + value > r.max_gross_exposure_inr:
            return self._deny(f"exposure: ₹{exposure + value:,.0f} would exceed ₹{r.max_gross_exposure_inr:,.0f}", record)
        if r.margin_check and self.funds_available_inr is not None:
            factor = self._margin_factor(inst, order)
            if factor is not None and value * factor * r.margin_buffer > self.funds_available_inr:
                return self._deny(f"margin: needs ~₹{value * factor:,.0f}, available ₹{self.funds_available_inr:,.0f}", record)
        key = (inst.key, order.side.value, order.qty)
        window = r.duplicate_window_ms / 1000.0
        while self._recent and now - self._recent[0][0] > window:
            self._recent.popleft()
        if any(k == key[0] and s == key[1] and q == key[2] for _, k, s, q in self._recent):
            return self._deny(f"duplicate: same order within {r.duplicate_window_ms}ms", record)
        if self.orders_today >= r.max_orders_per_day:
            return self._deny(f"max_orders_per_day: {r.max_orders_per_day} reached", record)
        if self.working_entries(inst) >= r.max_working_entries_per_instrument:
            return self._deny(f"working_entry: an entry order for {lim.alias} is already working", record)
        if self.entries_by_instrument.get(inst.key, 0) >= r.max_trades_per_instrument:
            return self._deny(f"max_trades_per_instrument: {r.max_trades_per_instrument} reached for {lim.alias}", record)
        for fn in self.extra_checks:
            try:
                d = fn(order, record)
            except Exception as e:  # a broken plug-in fails closed for entries (exits never reach here)
                log.exception("extra risk check %r raised", fn)
                return self._deny(f"extra_check: {type(e).__name__}: {e}", record)
            if d is not None and not d.ok:
                return self._deny(d.reason, record)
        if not record:
            return OK
        self._recent.append((now, inst.key, order.side.value, order.qty))
        self.orders_today += 1
        self.entries_by_instrument[inst.key] = self.entries_by_instrument.get(inst.key, 0) + 1
        return OK

    # ---- feedback from the OMS / engine ------------------------------------------------------------
    def on_trade_closed(self, trade: ClosedTrade, order: Order | None = None) -> None:
        self.closed_trades += 1
        if trade.pnl_paise < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.r.max_consecutive_losses:
                self.ks.pause(f"{self.consecutive_losses} consecutive losing trades")
        else:
            self.wins += 1
            self.consecutive_losses = 0

    def on_broker_reject(self, order: Order) -> None:
        now = time.monotonic()
        self._rejects.append(now)
        while self._rejects and now - self._rejects[0] > self.r.reject_storm_window_s:
            self._rejects.popleft()
        if len(self._rejects) >= self.r.reject_storm_count:
            self.ks.halt(f"reject storm: {len(self._rejects)} broker rejects within {self.r.reject_storm_window_s:.0f}s "
                         f"(last: {order.reason})")

    def check_pnl(self) -> None:
        """Call after marks / fills. Breaching the daily loss limit HALTS trading."""
        net = self.net_pnl_inr()
        self.peak_net_inr = max(self.peak_net_inr, net)
        self.min_net_inr = min(self.min_net_inr, net)
        if net <= -self.r.max_daily_loss_inr:
            self.ks.halt(f"daily loss limit hit: net P&L ₹{net:,.2f} <= -₹{self.r.max_daily_loss_inr:,.0f}")
        elif self.r.daily_profit_lock_inr > 0 and net >= self.r.daily_profit_lock_inr:
            self.ks.pause(f"daily profit lock reached: ₹{net:,.2f}")

    def reset_day(self) -> None:
        self.orders_today = 0
        self.entries_by_instrument.clear()
        self.consecutive_losses = 0
        self._recent.clear()
        self._rejects.clear()

    def status(self) -> dict:
        return {
            "state": self.ks.state.value, "reason": self.ks.reason, "net_pnl_inr": round(self.net_pnl_inr(), 2),
            "max_daily_loss_inr": self.r.max_daily_loss_inr, "orders_today": self.orders_today,
            "consecutive_losses": self.consecutive_losses, "closed_trades": self.closed_trades, "wins": self.wins,
            "funds_available_inr": self.funds_available_inr, "rejections": dict(self.rejections),
        }
