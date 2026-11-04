"""Spin up a set of bots in one process on a shared bus - the same classes the supervisor runs."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

from dhanbot.clock import epoch_at, ist_datetime

from tradingbots.bots import bot_class
from tradingbots.core.bus import MemoryBus
from tradingbots.core.runtime import BotContext, MarketCalendar, Paths, SharedSimClock
from tradingbots.core.settings import Settings

from .helpers import make_settings, wait_for


def next_weekday_at(hhmm: str = "09:20") -> float:
    d = ist_datetime(time.time()).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return epoch_at(d, hhmm)


class Team:
    def __init__(self, tmp: Path, names: list[str], settings: Settings | None = None, speed: float = 30.0,
                 start: str = "09:20", namespace: str = "team", bus_factory=None) -> None:
        self.tmp = tmp
        self.settings = settings or make_settings(tmp)
        self.clock = SharedSimClock(next_weekday_at(start), speed)
        self.paths = Paths(tmp, self.settings)
        self.namespace = namespace
        self.bus_factory = bus_factory or (lambda name: MemoryBus(name, namespace))
        self.bots = {}
        self.tasks = {}
        self.names = names
        self.client = self.bus_factory("test-client")

    def ctx(self, name: str) -> BotContext:
        return BotContext(bus=self.bus_factory(name), paths=self.paths, clock=self.clock, bootstrap=self.settings,
                          calendar=MarketCalendar())

    async def start(self, *names: str) -> None:
        await self.client.connect()
        for n in names or self.names:
            ctx = self.ctx(n)
            await ctx.bus.connect()
            bot = bot_class(n)(ctx)
            self.bots[n] = bot
            self.tasks[n] = asyncio.create_task(bot.run(), name=f"bot-{n}")
            assert await wait_for(lambda b=bot: b.status in ("ok", "failed"), 20), f"{n} did not start"
            assert bot.status == "ok", f"{n} failed: {bot.last_error}"

    async def stop(self, *names: str) -> None:
        for n in reversed(names or list(self.bots)):
            bot = self.bots.pop(n, None)
            if bot is None:
                continue
            bot.stop()
            try:
                await asyncio.wait_for(self.tasks.pop(n), 20)
            except Exception:
                pass
            await bot.bus.close()

    async def __aenter__(self) -> "Team":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()
        await self.client.close()
