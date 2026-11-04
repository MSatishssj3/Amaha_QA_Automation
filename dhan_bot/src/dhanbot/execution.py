"""Order pricing helpers (pure functions, used on the hot path)."""

from __future__ import annotations

from .market_state import InstrumentState
from .models import Instrument, Side


def clamp_circuit(paise: int, st: InstrumentState | None, inst: Instrument) -> int:
    if st is None:
        return paise
    if st.upper_circuit > 0:
        paise = min(paise, inst.round_to_tick(st.upper_circuit * 100, "down"))
    if st.lower_circuit > 0:
        paise = max(paise, inst.round_to_tick(st.lower_circuit * 100, "up"))
    return paise


def marketable_price(inst: Instrument, st: InstrumentState | None, side: Side, buffer_ticks: int) -> int:
    """A limit price that should execute immediately but caps slippage.

    BUY: best ask (or LTP without a book) + N ticks, rounded up to tick; SELL mirrors it.
    Clamped to the day's circuit limits so the exchange does not reject it. Returns 0 when there is
    no price at all yet (caller must reject).
    """
    if st is None or st.ltp <= 0:
        return 0
    if side is Side.BUY:
        ref = st.ask if st.ask > 0 else st.ltp
        px = inst.round_to_tick(ref * 100 + buffer_ticks * inst.tick_paise, "up")
    else:
        ref = st.bid if st.bid > 0 else st.ltp
        px = inst.round_to_tick(ref * 100 - buffer_ticks * inst.tick_paise, "down")
    px = max(px, inst.tick_paise)
    return clamp_circuit(px, st, inst)


def user_limit_price(inst: Instrument, price: float, side: Side) -> int:
    """Round a user-supplied limit to tick in the *conservative* direction (never pay more)."""
    return inst.round_to_tick(price * 100, "down" if side is Side.BUY else "up")


def sl_limit_price(inst: Instrument, trigger_paise: int, exit_side: Side, buffer_ticks: int) -> int:
    """Limit price for a stop-loss-limit exit: beyond the trigger by N ticks so it fills when hit."""
    if exit_side is Side.SELL:
        return max(inst.tick_paise, inst.round_to_tick(trigger_paise - buffer_ticks * inst.tick_paise, "down"))
    return inst.round_to_tick(trigger_paise + buffer_ticks * inst.tick_paise, "up")
