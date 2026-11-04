"""Replay / backtest: push recorded (or synthetic) feed frames through the real engine.

The engine runs in ``replay`` mode with a ``VirtualClock`` that jumps to each frame's receive time,
and the ``PaperBroker`` simulates fills - the same parser, bars, strategy, risk, OMS and positions
code that runs live. Results are deterministic for a given input.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from pathlib import Path
from typing import Iterable

from .clock import IST_OFFSET_S, VirtualClock, epoch_at
from .config import AppConfig
from .dhan import packets as P
from .engine import Engine
from .instruments import InstrumentStore
from .recorder import iter_frames
from .strategy.base import Strategy

log = logging.getLogger(__name__)


def synth_frames(symbols: list[str], day: date, minutes: int = 375, seed: int = 7, mode: str = "full",
                 start: str = "09:15", step_s: float = 1.0, ltt_ist_shifted: bool = True) -> list[tuple[int, bytes]]:
    """Generate a synthetic session for mock instruments (see ``sim.mock_dhan.default_instruments``)."""
    from .sim.mock_dhan import PriceSim, default_instruments

    insts = default_instruments(day)
    wanted = [i for i in insts if i.trading_symbol in symbols]
    missing = set(symbols) - {i.trading_symbol for i in wanted}
    if missing:
        raise ValueError(f"unknown synthetic symbols {sorted(missing)}; available: "
                         f"{[i.trading_symbol for i in insts if not i.option_type][:10]} + NIFTY options")
    sim = PriceSim(insts, seed)
    keys = {(i.segment, i.security_id) for i in wanted}
    t0 = epoch_at(day, start)
    out: list[tuple[int, bytes]] = []
    offset = IST_OFFSET_S if ltt_ist_shifted else 0
    out.append((int((t0 - 1) * 1e9), b"".join(P.build_prev_close(i.feed_code, i.security_id, i.prev_close)
                                              for i in wanted)))
    n = int(minutes * 60 / step_s)
    for k in range(n):
        t = t0 + k * step_s
        sim.step(step_s, keys)
        frame = bytearray()
        for i in wanted:
            ltt = int(t) + offset
            if mode == "ticker" or i.instrument_type == "INDEX":
                frame += P.build_ticker(i.feed_code, i.security_id, i.price, ltt)
            elif mode == "quote":
                frame += P.build_quote(i.feed_code, i.security_id, i.price, ltt, volume=i.volume, open=i.open,
                                       high=i.high, low=i.low, close=i.prev_close)
            else:
                frame += P.build_full(i.feed_code, i.security_id, i.price, ltt, volume=i.volume, open=i.open,
                                      high=i.high, low=i.low, close=i.prev_close, bids=i.depth_bids, asks=i.depth_asks)
        out.append((int(t * 1e9), bytes(frame)))
    return out


def synth_instrument_store(day: date) -> InstrumentStore:
    from .sim.mock_dhan import MockDhan, default_instruments

    m = MockDhan.__new__(MockDhan)
    m.instruments = default_instruments(day)
    return InstrumentStore.from_csv_text(MockDhan.instrument_csv(m))


async def run_replay(cfg: AppConfig, frames: Iterable[tuple[int, bytes]], *, instruments: InstrumentStore,
                     strategy: Strategy | None = None, speed: float | None = None, use_journal: bool = True,
                     tail_s: float = 5.0) -> dict:
    """Replay frames; ``speed=None`` runs as fast as possible, ``speed=N`` paces at N x real time."""
    it = iter(frames)
    try:
        first = next(it)
    except StopIteration:
        raise ValueError("no frames to replay") from None
    clock = VirtualClock(first[0] / 1e9 - 1)
    engine = Engine(cfg, mode="replay", clock=clock, instruments=instruments, strategy=strategy,
                    use_journal=use_journal, use_control=False, use_telegram=False, record=False)
    await engine.start()
    t_real0 = time.perf_counter()
    t_sim0 = first[0] / 1e9
    n = 0
    last = first[0] / 1e9

    def feed(ns: int, frame: bytes) -> None:
        clock.advance_to(ns / 1e9)
        engine.on_frame(frame)

    feed(*first)
    for ns, frame in it:
        n += 1
        last = ns / 1e9
        if speed:
            lag = (last - t_sim0) / speed - (time.perf_counter() - t_real0)
            if lag > 0:
                await asyncio.sleep(lag)
        feed(ns, frame)
        await asyncio.sleep(0)  # let order / exit tasks progress between frames
    for _ in range(int(tail_s * 4)):  # let timers (bar flush, acks) fire after the last frame
        clock.advance(0.25)
        await asyncio.sleep(0)
    engine.request_stop("replay finished")
    report = await engine.shutdown()
    report["frames"] = n + 1
    report["replayed_span_s"] = round(last - t_sim0, 1)
    return report


async def replay_file(cfg: AppConfig, path: Path, instruments: InstrumentStore, strategy: Strategy | None = None,
                      speed: float | None = None) -> dict:
    return await run_replay(cfg, iter_frames(path), instruments=instruments, strategy=strategy, speed=speed)
