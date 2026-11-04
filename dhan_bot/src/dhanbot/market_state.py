"""Latest market state per instrument (hot path: one dict lookup + attribute writes per tick)."""

from __future__ import annotations

from .dhan.packets import PrevClose, Tick
from .models import Instrument


class InstrumentState:
    __slots__ = ("inst", "ltp", "bid", "ask", "bid_qty", "ask_qty", "bids", "asks", "volume", "oi", "open", "high",
                 "low", "close", "prev_close", "atp", "ltt", "last_update", "ticks", "upper_circuit", "lower_circuit")

    def __init__(self, inst: Instrument) -> None:
        self.inst = inst
        self.ltp = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.bid_qty = 0
        self.ask_qty = 0
        self.bids: tuple = ()
        self.asks: tuple = ()
        self.volume = 0
        self.oi = 0
        self.open = self.high = self.low = self.close = self.prev_close = self.atp = 0.0
        self.ltt = 0.0
        self.last_update = 0.0  # clock time of last tick
        self.ticks = 0
        self.upper_circuit = 0.0
        self.lower_circuit = 0.0

    @property
    def ltp_paise(self) -> int:
        return int(round(self.ltp * 100))

    @property
    def has_book(self) -> bool:
        return self.bid > 0 and self.ask > 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.has_book else self.ltp

    def __repr__(self) -> str:
        return f"State({self.inst.trading_symbol} ltp={self.ltp} bid={self.bid} ask={self.ask} vol={self.volume})"


class MarketState:
    def __init__(self) -> None:
        self.states: dict[tuple[int, int], InstrumentState] = {}

    def register(self, inst: Instrument) -> InstrumentState:
        st = self.states.get(inst.key)
        if st is None:
            st = self.states[inst.key] = InstrumentState(inst)
        return st

    def get(self, key: tuple[int, int]) -> InstrumentState | None:
        return self.states.get(key)

    def of(self, inst: Instrument) -> InstrumentState | None:
        return self.states.get(inst.key)

    def on_tick(self, t: Tick, now: float, ltt_epoch: float) -> InstrumentState | None:
        st = self.states.get((t.seg, t.sid))
        if st is None:
            return None
        st.ltp = t.ltp
        st.ltt = ltt_epoch
        st.last_update = now
        st.ticks += 1
        if t.code != 2:  # quote / full carry session stats
            st.volume = t.volume
            st.atp = t.atp
            st.open, st.high, st.low, st.close = t.open, t.high, t.low, t.close
            if t.bids:
                b, a = t.bids[0], t.asks[0]
                st.bid, st.bid_qty = b[0], b[1]
                st.ask, st.ask_qty = a[0], a[1]
                st.bids, st.asks = t.bids, t.asks
                st.oi = t.oi
        elif st.inst.segment == "IDX_I":
            st.bid = st.ask = 0.0
        return st

    def on_prev_close(self, p: PrevClose) -> None:
        st = self.states.get((p.seg, p.sid))
        if st is not None:
            st.prev_close = p.prev_close

    def age(self, inst: Instrument, now: float) -> float:
        st = self.states.get(inst.key)
        if st is None or st.last_update == 0:
            return float("inf")
        return now - st.last_update
