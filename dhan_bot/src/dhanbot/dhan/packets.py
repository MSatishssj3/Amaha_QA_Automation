"""Binary market-feed packet parser (DhanHQ v2 live market feed) + packet builders.

Layouts are taken from the official ``dhanhq`` 2.3.0 SDK (``marketfeed.py``). Every packet starts
with an 8-byte little-endian header ``<BHBI``: response code, message length, exchange-segment
code, security id. Sizes (bytes): ticker 16, quote 50, OI 12, prev-close 16, market-status 8,
full 162, disconnect 10.

Differences from the SDK, all for speed or robustness:
* precompiled ``struct.Struct`` + ``unpack_from`` (no slicing / copying);
* numeric fields stay numbers (the SDK formats prices into strings);
* a single WebSocket frame may carry several packets - we iterate the whole frame;
* unknown packet codes are reported once and the rest of the frame is skipped instead of crashing.

Parity with the SDK parser is enforced by ``tests/test_packets.py``.
"""

from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)

CODE_TICKER = 2
CODE_DEPTH_V1 = 3
CODE_QUOTE = 4
CODE_OI = 5
CODE_PREV_CLOSE = 6
CODE_STATUS = 7
CODE_FULL = 8
CODE_DISCONNECT = 50

HEADER = struct.Struct("<BHBI")  # 8
_TICKER = struct.Struct("<BHBIfI")  # 16: ltp, ltt
_QUOTE = struct.Struct("<BHBIfHIfIIIffff")  # 50: ltp ltq ltt atp vol tsq tbq open close high low
_OI = struct.Struct("<BHBII")  # 12: oi
_PREV = struct.Struct("<BHBIfI")  # 16: prev_close prev_oi
_FULL = struct.Struct("<BHBIfHIfIIIIIIffff")  # 62: ltp ltq ltt atp vol tsq tbq oi oi_hi oi_lo open close high low
_DEPTH5 = struct.Struct("<" + "IIHHff" * 5)  # 100: 5 x (bid_qty ask_qty bid_orders ask_orders bid_px ask_px)
_DISC = struct.Struct("<BHBIH")  # 10: reason

SIZES = {
    CODE_TICKER: 16,
    CODE_QUOTE: 50,
    CODE_OI: 12,
    CODE_PREV_CLOSE: 16,
    CODE_STATUS: 8,
    CODE_FULL: 162,
    CODE_DISCONNECT: 10,
}
_FULL_DEPTH_OFFSET = _FULL.size  # 62


class Tick:
    """A price update (ticker / quote / full packet). Absent fields are 0 / empty."""

    __slots__ = ("code", "seg", "sid", "ltp", "ltt", "ltq", "atp", "volume", "tot_sell", "tot_buy",
                 "open", "high", "low", "close", "oi", "oi_high", "oi_low", "bids", "asks", "recv_ns")

    def __init__(self, code: int, seg: int, sid: int, ltp: float, ltt: int, recv_ns: int = 0) -> None:
        self.code = code
        self.seg = seg
        self.sid = sid
        self.ltp = ltp
        self.ltt = ltt  # raw feed value; normalise with clock.LttNormalizer
        self.recv_ns = recv_ns
        self.ltq = 0
        self.atp = 0.0
        self.volume = 0
        self.tot_sell = 0
        self.tot_buy = 0
        self.open = 0.0
        self.high = 0.0
        self.low = 0.0
        self.close = 0.0
        self.oi = 0
        self.oi_high = 0
        self.oi_low = 0
        self.bids: tuple = ()  # ((price, qty, orders), ...) best first
        self.asks: tuple = ()

    @property
    def key(self) -> tuple[int, int]:
        return (self.seg, self.sid)

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    def __repr__(self) -> str:
        return f"Tick(code={self.code} seg={self.seg} sid={self.sid} ltp={self.ltp} ltt={self.ltt} vol={self.volume})"


class OIUpdate:
    __slots__ = ("seg", "sid", "oi")

    def __init__(self, seg: int, sid: int, oi: int) -> None:
        self.seg, self.sid, self.oi = seg, sid, oi


class PrevClose:
    __slots__ = ("seg", "sid", "prev_close", "prev_oi")

    def __init__(self, seg: int, sid: int, prev_close: float, prev_oi: int) -> None:
        self.seg, self.sid, self.prev_close, self.prev_oi = seg, sid, prev_close, prev_oi


class MarketStatus:
    __slots__ = ("seg", "sid")

    def __init__(self, seg: int, sid: int) -> None:
        self.seg, self.sid = seg, sid


class Disconnect:
    __slots__ = ("reason",)

    def __init__(self, reason: int) -> None:
        self.reason = reason


_warned_codes: set[int] = set()


def parse_frame(buf: bytes, recv_ns: int = 0) -> list:
    """Parse one WebSocket binary frame into packet objects (in order)."""
    out: list = []
    n = len(buf)
    off = 0
    while off + 8 <= n:
        code = buf[off]
        size = SIZES.get(code)
        if size is None:
            if code not in _warned_codes:
                _warned_codes.add(code)
                log.warning("feed: unknown packet code %d (frame len %d) - skipping rest of frame", code, n)
            break
        if off + size > n:
            log.warning("feed: truncated packet code %d at offset %d (frame len %d)", code, off, n)
            break
        if code == CODE_TICKER:
            _, mlen, seg, sid, ltp, ltt = _TICKER.unpack_from(buf, off)
            out.append(Tick(code, seg, sid, round(ltp, 2), ltt, recv_ns))
        elif code == CODE_QUOTE:
            (_, mlen, seg, sid, ltp, ltq, ltt, atp, vol, tsq, tbq, o, c, h, lo) = _QUOTE.unpack_from(buf, off)
            t = Tick(code, seg, sid, round(ltp, 2), ltt, recv_ns)
            t.ltq, t.atp, t.volume, t.tot_sell, t.tot_buy = ltq, round(atp, 2), vol, tsq, tbq
            t.open, t.close, t.high, t.low = round(o, 2), round(c, 2), round(h, 2), round(lo, 2)
            out.append(t)
        elif code == CODE_FULL:
            (_, mlen, seg, sid, ltp, ltq, ltt, atp, vol, tsq, tbq, oi, oih, oil, o, c, h, lo) = _FULL.unpack_from(buf, off)
            t = Tick(code, seg, sid, round(ltp, 2), ltt, recv_ns)
            t.ltq, t.atp, t.volume, t.tot_sell, t.tot_buy = ltq, round(atp, 2), vol, tsq, tbq
            t.oi, t.oi_high, t.oi_low = oi, oih, oil
            t.open, t.close, t.high, t.low = round(o, 2), round(c, 2), round(h, 2), round(lo, 2)
            d = _DEPTH5.unpack_from(buf, off + _FULL_DEPTH_OFFSET)
            # d = (bq0, aq0, bo0, ao0, bp0, ap0, bq1, ...)
            t.bids = tuple((round(d[i + 4], 2), d[i], d[i + 2]) for i in range(0, 30, 6))
            t.asks = tuple((round(d[i + 5], 2), d[i + 1], d[i + 3]) for i in range(0, 30, 6))
            out.append(t)
        elif code == CODE_OI:
            _, mlen, seg, sid, oi = _OI.unpack_from(buf, off)
            out.append(OIUpdate(seg, sid, oi))
        elif code == CODE_PREV_CLOSE:
            _, mlen, seg, sid, pc, poi = _PREV.unpack_from(buf, off)
            out.append(PrevClose(seg, sid, round(pc, 2), poi))
        elif code == CODE_STATUS:
            _, mlen, seg, sid = HEADER.unpack_from(buf, off)
            out.append(MarketStatus(seg, sid))
        else:  # CODE_DISCONNECT
            _, mlen, seg, sid, reason = _DISC.unpack_from(buf, off)
            out.append(Disconnect(reason))
        # Prefer the header's length when it is self-consistent (future-proof if Dhan appends
        # fields); otherwise step by the known fixed size.
        off += mlen if (mlen >= size and off + mlen <= n) else size
    return out


# ---------------------------------------------------------------------------------------------
# builders (tests, replay synthesis, mock server)
# ---------------------------------------------------------------------------------------------


def build_ticker(seg: int, sid: int, ltp: float, ltt: int) -> bytes:
    return _TICKER.pack(CODE_TICKER, 16, seg, sid, ltp, ltt)


def build_quote(seg: int, sid: int, ltp: float, ltt: int, *, ltq: int = 0, atp: float = 0.0, volume: int = 0,
                tot_sell: int = 0, tot_buy: int = 0, open: float = 0.0, close: float = 0.0, high: float = 0.0,
                low: float = 0.0) -> bytes:
    return _QUOTE.pack(CODE_QUOTE, 50, seg, sid, ltp, ltq, ltt, atp, volume, tot_sell, tot_buy, open, close, high, low)


def build_full(seg: int, sid: int, ltp: float, ltt: int, *, ltq: int = 0, atp: float = 0.0, volume: int = 0,
               tot_sell: int = 0, tot_buy: int = 0, oi: int = 0, oi_high: int = 0, oi_low: int = 0,
               open: float = 0.0, close: float = 0.0, high: float = 0.0, low: float = 0.0,
               bids: list[tuple[float, int, int]] | None = None,
               asks: list[tuple[float, int, int]] | None = None) -> bytes:
    bids = list(bids or [])[:5] + [(0.0, 0, 0)] * (5 - len(bids or []))
    asks = list(asks or [])[:5] + [(0.0, 0, 0)] * (5 - len(asks or []))
    head = _FULL.pack(CODE_FULL, 162, seg, sid, ltp, ltq, ltt, atp, volume, tot_sell, tot_buy, oi, oi_high, oi_low,
                      open, close, high, low)
    flat: list = []
    for (bp, bq, bo), (ap, aq, ao) in zip(bids, asks, strict=True):
        flat.extend((bq, aq, bo, ao, bp, ap))
    return head + _DEPTH5.pack(*flat)


def build_oi(seg: int, sid: int, oi: int) -> bytes:
    return _OI.pack(CODE_OI, 12, seg, sid, oi)


def build_prev_close(seg: int, sid: int, prev_close: float, prev_oi: int = 0) -> bytes:
    return _PREV.pack(CODE_PREV_CLOSE, 16, seg, sid, prev_close, prev_oi)


def build_status(seg: int = 0, sid: int = 0) -> bytes:
    return HEADER.pack(CODE_STATUS, 8, seg, sid)


def build_disconnect(reason: int) -> bytes:
    return _DISC.pack(CODE_DISCONNECT, 10, 0, 0, reason)
