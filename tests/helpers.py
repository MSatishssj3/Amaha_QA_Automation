from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

from dhanbot.clock import RealClock

from tradingbots.core.bus import Bus, MemoryBus
from tradingbots.core.runtime import BotContext, MarketCalendar, Paths
from tradingbots.core.settings import Settings, parse_settings


async def wait_for(cond: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = cond()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return True
        await asyncio.sleep(interval)
    r = cond()
    if asyncio.iscoroutine(r):
        r = await r
    return bool(r)


def make_settings(tmp: Path, **sections: Any) -> Settings:
    data: dict[str, Any] = {"system": {"data_dir": str(tmp / "data"), "log_dir": str(tmp / "logs")}}
    for k, v in sections.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k].update(v)
        else:
            data[k] = v
    return parse_settings(data)


def make_ctx(tmp: Path, bus: Bus | None = None, settings: Settings | None = None, clock=None,
             name: str = "test") -> BotContext:
    s = settings or make_settings(tmp)
    return BotContext(bus=bus or MemoryBus(name), paths=Paths(tmp, s), clock=clock or RealClock(), bootstrap=s,
                      calendar=MarketCalendar())
