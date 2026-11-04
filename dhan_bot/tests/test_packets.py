"""Binary feed parser: golden bytes, multi-packet frames, robustness, and parity with the official SDK."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from dhanbot.dhan import packets as P


def test_sizes_match_struct_layouts():
    assert len(P.build_ticker(1, 1333, 1.0, 0)) == 16
    assert len(P.build_quote(1, 1333, 1.0, 0)) == 50
    assert len(P.build_oi(2, 1, 5)) == 12
    assert len(P.build_prev_close(1, 1, 1.0)) == 16
    assert len(P.build_status()) == 8
    assert len(P.build_full(2, 1, 1.0, 0)) == 162
    assert len(P.build_disconnect(807)) == 10


def test_golden_ticker_bytes():
    # code=2, len=16, seg=1 (NSE_EQ), sid=1333, ltp=1650.25f, ltt=1730000000
    raw = bytes.fromhex("02100001" "35050000" "0048ce44" "80b41d67")
    assert P.build_ticker(1, 1333, 1650.25, 1730000000) == raw
    (t,) = P.parse_frame(raw, recv_ns=7)
    assert isinstance(t, P.Tick)
    assert (t.code, t.seg, t.sid, t.ltp, t.ltt, t.recv_ns) == (2, 1, 1333, 1650.25, 1730000000, 7)


def test_quote_fields():
    raw = P.build_quote(1, 11536, 2534.45, 1730000123, ltq=10, atp=2530.1, volume=123456, tot_sell=500,
                        tot_buy=700, open=2500.0, close=2490.5, high=2540.0, low=2495.55)
    (t,) = P.parse_frame(raw)
    assert t.ltp == 2534.45 and t.ltq == 10 and t.atp == 2530.1 and t.volume == 123456
    assert (t.tot_sell, t.tot_buy) == (500, 700)
    assert (t.open, t.close, t.high, t.low) == (2500.0, 2490.5, 2540.0, 2495.55)


def test_full_with_depth():
    bids = [(100.05 - i * 0.05, 10 + i, 1 + i) for i in range(5)]
    asks = [(100.10 + i * 0.05, 20 + i, 2 + i) for i in range(5)]
    raw = P.build_full(2, 52175, 100.1, 1730000000, volume=999, oi=12345, oi_high=13000, oi_low=12000,
                       open=95.0, close=90.0, high=101.0, low=94.0, bids=bids, asks=asks)
    (t,) = P.parse_frame(raw)
    assert t.code == P.CODE_FULL and t.oi == 12345 and (t.oi_high, t.oi_low) == (13000, 12000)
    assert t.bids[0] == (100.05, 10, 1) and t.asks[0] == (100.1, 20, 2)
    assert t.bids[4] == (round(100.05 - 0.2, 2), 14, 5)
    assert t.best_bid == 100.05 and t.best_ask == 100.1


def test_multi_packet_frame_and_other_types():
    frame = (P.build_ticker(1, 1, 10.0, 1) + P.build_oi(2, 2, 77) + P.build_prev_close(1, 1, 9.5, 3)
             + P.build_status(0, 0) + P.build_quote(1, 3, 11.0, 2) + P.build_disconnect(807))
    out = P.parse_frame(frame)
    kinds = [type(x).__name__ for x in out]
    assert kinds == ["Tick", "OIUpdate", "PrevClose", "MarketStatus", "Tick", "Disconnect"]
    assert out[1].oi == 77 and out[2].prev_close == 9.5 and out[5].reason == 807


def test_truncated_and_unknown_codes_do_not_raise():
    good = P.build_ticker(1, 1, 10.0, 1)
    assert len(P.parse_frame(good + good[:9])) == 1  # trailing partial packet dropped
    assert len(P.parse_frame(good + bytes([99]) + b"\x00" * 20 + good)) == 1  # unknown code stops frame
    assert P.parse_frame(b"") == []
    assert P.parse_frame(b"\x02\x00") == []


def test_header_length_longer_than_known_size_is_honoured():
    # If Dhan appends fields to a packet, the header length tells us where the next packet starts.
    t = bytearray(P.build_ticker(1, 1, 10.0, 1))
    t[1:3] = (20).to_bytes(2, "little")
    frame = bytes(t) + b"\x00" * 4 + P.build_ticker(1, 2, 11.0, 1)
    out = P.parse_frame(frame)
    assert [x.sid for x in out] == [1, 2]


# ---------------------------------------------------------------------------------------------
# parity with the official SDK parser (dhanhq 2.3.0)
# ---------------------------------------------------------------------------------------------

dhanhq = pytest.importorskip("dhanhq")


def _sdk():
    mf = object.__new__(dhanhq.MarketFeed)  # skip __init__ (it creates an event loop + needs credentials)
    mf.on_close = None
    return mf


def _hhmmss(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M:%S")


@pytest.mark.parametrize("seed", range(25))
def test_parity_with_sdk(seed):
    rng = random.Random(seed)
    sdk = _sdk()
    seg = rng.choice([0, 1, 2, 3, 4, 5, 7, 8])
    sid = rng.randint(1, 2_000_000)
    ltp = round(rng.uniform(1, 90000), 2)
    ltt = rng.randint(1_700_000_000, 1_900_000_000)

    raw = P.build_ticker(seg, sid, ltp, ltt)
    (ours,), theirs = P.parse_frame(raw), sdk.process_data(raw)
    assert theirs["exchange_segment"] == ours.seg and theirs["security_id"] == ours.sid
    assert theirs["LTP"] == f"{ours.ltp:.2f}" and theirs["LTT"] == _hhmmss(ours.ltt)

    kw = dict(ltq=rng.randint(0, 65535), atp=round(rng.uniform(1, 9000), 2), volume=rng.randint(0, 2**31),
              tot_sell=rng.randint(0, 2**31), tot_buy=rng.randint(0, 2**31), open=round(rng.uniform(1, 9000), 2),
              close=round(rng.uniform(1, 9000), 2), high=round(rng.uniform(1, 9000), 2), low=round(rng.uniform(1, 9000), 2))
    raw = P.build_quote(seg, sid, ltp, ltt, **kw)
    (ours,), theirs = P.parse_frame(raw), sdk.process_data(raw)
    assert theirs["LTP"] == f"{ours.ltp:.2f}" and theirs["LTQ"] == ours.ltq and theirs["volume"] == ours.volume
    assert theirs["avg_price"] == f"{ours.atp:.2f}"
    assert theirs["total_sell_quantity"] == ours.tot_sell and theirs["total_buy_quantity"] == ours.tot_buy
    for k, v in (("open", ours.open), ("close", ours.close), ("high", ours.high), ("low", ours.low)):
        assert theirs[k] == f"{v:.2f}"

    bids = [(round(rng.uniform(1, 9000), 2), rng.randint(0, 10**6), rng.randint(0, 999)) for _ in range(5)]
    asks = [(round(rng.uniform(1, 9000), 2), rng.randint(0, 10**6), rng.randint(0, 999)) for _ in range(5)]
    raw = P.build_full(seg, sid, ltp, ltt, oi=rng.randint(0, 2**31), oi_high=rng.randint(0, 2**31),
                       oi_low=rng.randint(0, 2**31), bids=bids, asks=asks, **kw)
    (ours,), theirs = P.parse_frame(raw), sdk.process_data(raw)
    assert theirs["OI"] == ours.oi and theirs["oi_day_high"] == ours.oi_high and theirs["oi_day_low"] == ours.oi_low
    for lvl, d in enumerate(theirs["depth"]):
        assert d["bid_price"] == f"{ours.bids[lvl][0]:.2f}" and d["ask_price"] == f"{ours.asks[lvl][0]:.2f}"
        assert d["bid_quantity"] == ours.bids[lvl][1] and d["ask_quantity"] == ours.asks[lvl][1]
        assert d["bid_orders"] == ours.bids[lvl][2] and d["ask_orders"] == ours.asks[lvl][2]

    raw = P.build_oi(seg, sid, 4242)
    assert sdk.process_data(raw)["OI"] == P.parse_frame(raw)[0].oi == 4242
    raw = P.build_prev_close(seg, sid, ltp, 99)
    theirs, (ours,) = sdk.process_data(raw), P.parse_frame(raw)
    assert theirs["prev_close"] == f"{ours.prev_close:.2f}" and theirs["prev_OI"] == ours.prev_oi == 99
