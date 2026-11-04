"""Journal, tick recorder, latency stats, Telegram command handling, and a hot-path benchmark guard."""

from __future__ import annotations

import time
from datetime import date

from dhanbot.clock import VirtualClock, epoch_at
from dhanbot.config import load_config
from dhanbot.dhan import packets as P
from dhanbot.engine import Engine
from dhanbot.journal import Journal
from dhanbot.latency import LatencyTracker, format_stats
from dhanbot.models import Fill, Instrument, Order, OrderStatus, Product, Side
from dhanbot.notify.telegram import Telegram, format_reply
from dhanbot.recorder import TickRecorder, iter_frames, write_frames
from dhanbot.replay import synth_instrument_store

INST = Instrument(2885, "NSE_EQ", 1, "RELIANCE", tick_paise=10)


async def test_journal_round_trip(tmp_path):
    j = Journal(tmp_path / "j.db")
    await j.open("paper", "test", "abc")
    o = Order(1, INST, Side.BUY, 5, price_paise=145000, cid="DB2611040000AB", broker_id="B1")
    j.order(o)
    o.status, o.filled_qty, o.avg_fill_paise = OrderStatus.FILLED, 5, 145010.0
    j.order(o)  # upsert
    j.fill(Fill(1, INST, Product.INTRADAY, Side.BUY, 5, 145010.0, "MAIN", time.time()), o)
    j.event("alert", {"text": "hello"})
    j.pnl(time.time(), 1.0, 2.0, 0.5, 2.5, 1)
    await j.flush()
    rows = await j.query("SELECT status, filled_qty, avg_price FROM orders")
    assert rows == [("FILLED", 5, 1450.1)]
    assert (await j.query("SELECT COUNT(*) FROM fills"))[0][0] == 1
    assert await j.max_cid_seq("DB261104") == int("0000AB", 36)
    await j.close()


async def test_recorder_round_trip(tmp_path):
    rec = TickRecorder(tmp_path / "t.bin")
    frames = [P.build_ticker(1, 1, 10.0 + i, i) for i in range(10)]
    for i, f in enumerate(frames):
        rec.add(f, wall_ns=1_000_000_000 * (i + 1))
    await rec.close()
    back = list(iter_frames(tmp_path / "t.bin"))
    assert [f for _, f in back] == frames and back[0][0] == 1_000_000_000
    write_frames(tmp_path / "w.bin", back[:3])
    with (tmp_path / "w.bin").open("ab") as fh:
        fh.write(b"\x01\x02")  # truncated trailing record is ignored
    assert len(list(iter_frames(tmp_path / "w.bin"))) == 3


def test_latency_stats():
    lt = LatencyTracker()
    for v in range(1, 101):
        lt.record("x", v * 1000)
    stats = lt.roll()
    assert stats[0].n == 100 and stats[0].p50_us == 50.0 and stats[0].max_us == 100.0
    assert lt.roll() == [] and lt.day()[0].n == 100
    assert "x" in format_stats(lt.snapshot())


async def test_telegram_commands_and_confirmation():
    calls = []
    tg = Telegram("t", "42", {"status": lambda r: {"state": "RUNNING"},
                              "flatten": lambda r: calls.append("flatten") or {"flattened": True},
                              "positions": lambda r: {"positions": ["a", "b"]}})
    assert "RUNNING" in await tg.handle_text("/status")
    assert "/confirm" in await tg.handle_text("/flatten")
    assert calls == []
    assert "flattened: True" in await tg.handle_text("/confirm")
    assert calls == ["flatten"]
    assert "nothing to confirm" in await tg.handle_text("/confirm")
    assert "unknown command" in await tg.handle_text("/bogus")
    assert "/pause" in await tg.handle_text("/help")
    assert "  a" in format_reply("positions", {"positions": ["a"]})
    tg.send("x" * 10000)
    assert tg._q.qsize() == 1 and len(tg._q.get_nowait()) <= 3900


async def test_hot_path_benchmark_guard(tmp_path):
    """Not a precise benchmark - a regression guard with generous limits (CI machines vary)."""
    day = date(2026, 11, 4)
    cfg = load_config(data={"paths": {"data_dir": str(tmp_path)}, "perf": {"gc_freeze": False},
                            "instruments": [{"alias": "RELIANCE", "segment": "NSE_EQ", "trading_symbol": "RELIANCE",
                                             "mode": "full", "max_qty": 100}],
                            "risk": {"max_orders_per_day": 10**6, "max_trades_per_instrument": 10**6,
                                     "duplicate_window_ms": 0, "max_gross_exposure_inr": 1e12,
                                     "max_open_positions": 100}}, env={})
    clock = VirtualClock(epoch_at(day, "09:30"))
    e = Engine(cfg, mode="replay", clock=clock, instruments=synth_instrument_store(day), use_journal=False,
               use_control=False, use_telegram=False, record=False)
    await e.start()
    frame = P.build_full(1, 2885, 1450.1, 0, volume=1000, bids=[(1450.0, 10, 1)] * 5, asks=[(1450.2, 10, 1)] * 5)
    n = 5000
    t0 = time.perf_counter_ns()
    for _ in range(n):
        P.parse_frame(frame)
    parse_us = (time.perf_counter_ns() - t0) / n / 1000
    t0 = time.perf_counter_ns()
    for _ in range(n):
        e.on_frame(frame)
    frame_us = (time.perf_counter_ns() - t0) / n / 1000
    e.limiter.sec.limit = e.limiter.min.limit = 10**9
    m = 300
    t0 = time.perf_counter_ns()
    for _ in range(m):
        e.place(Side.BUY, "RELIANCE", 1)
    place_us = (time.perf_counter_ns() - t0) / m / 1000
    print(f"\nparse_frame(full) {parse_us:.1f}us  on_frame {frame_us:.1f}us  place {place_us:.1f}us")
    assert parse_us < 60 and frame_us < 150 and place_us < 600
    e.request_stop("bench")
    await e.shutdown()
