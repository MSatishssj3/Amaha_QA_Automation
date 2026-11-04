"""Base class for every bot: lifecycle, subscriptions, commands, scheduled jobs, heartbeats, settings.

A bot declares what it listens to and what it does; the base class runs it:

    class MyBot(Bot):
        name = "mybot"

        def configure(self):
            self.on(topics.MARKET_BARS, self.handle_bars)          # event handler
            self.command("refresh", self.cmd_refresh)              # request/reply: cmd.mybot.refresh
            self.every(60, self.job, name="minutely")              # interval job
            self.daily("16:00", self.eod, name="end-of-day")       # IST wall-clock job (trading days)

Handlers run one at a time in arrival order. A handler that raises is logged and counted - the bot
keeps running (the supervisor restarts it only if the whole process dies). Long work belongs in
``self.spawn(...)`` so the inbox keeps moving.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable

from . import topics
from .bus import Bus, Message
from .runtime import BotContext
from .settings import Settings, SettingsError, parse_settings

Handler = Callable[[Message], Awaitable[None]]
CommandHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Bot:
    name = "bot"
    heartbeat_s = 5.0
    start_from = "latest"  # where a brand-new consumer group starts ("latest" or "earliest")

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx
        self.bus: Bus = ctx.bus
        self.clock = ctx.clock
        self.log = logging.getLogger(f"bot.{self.name}")
        self.settings: Settings = ctx.bootstrap
        self.settings_version = 0
        self._handlers: dict[str, list[Handler]] = {}
        self._commands: dict[str, CommandHandler] = {}
        self._jobs: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self.started_at = 0.0
        self.status = "starting"
        self.counters: dict[str, int] = {"messages": 0, "errors": 0, "commands": 0}
        self.last_error = ""
        self.on(topics.CONFIG_CHANGED, self._on_config_changed)
        self.configure()

    # ---- declaration API ----------------------------------------------------------------------
    def configure(self) -> None:
        """Override: register handlers, commands and jobs."""

    def on(self, topic: str, fn: Handler) -> None:
        self._handlers.setdefault(topic, []).append(fn)

    def command(self, action: str, fn: CommandHandler) -> None:
        self._commands[topics.cmd(self.name, action)] = fn

    def every(self, seconds: float, fn: Callable[[], Awaitable[Any]], *, name: str, run_at_start: bool = False,
              session_only: bool = False) -> None:
        async def loop() -> None:
            if not run_at_start:
                await self.clock.sleep(seconds)
            while not self._stop.is_set():
                if not session_only or self.ctx.calendar.in_session(self.clock.time()):
                    await self._run_job(name, fn)
                await self.clock.sleep(seconds)

        self._jobs.append((name, loop))

    def daily(self, hhmm: str | Callable[[], str], fn: Callable[[], Awaitable[Any]], *, name: str,
              trading_days_only: bool = True) -> None:
        async def loop() -> None:
            while not self._stop.is_set():
                at = hhmm() if callable(hhmm) else hhmm
                nxt = self.ctx.calendar.next_time(self.clock.time(), at, trading_days_only)
                await self.clock.sleep(max(0.0, nxt - self.clock.time()))
                if not self._stop.is_set():
                    await self._run_job(name, fn)

        self._jobs.append((name, loop))

    # ---- lifecycle hooks ---------------------------------------------------------------------------
    async def setup(self) -> None:
        """Override: open stores, connect to services. Settings are already loaded."""

    async def teardown(self) -> None:
        """Override: flush and close."""

    async def on_settings(self, old: Settings, new: Settings) -> None:
        """Override: react to a settings change (``self.settings`` is already ``new``)."""

    def details(self) -> dict[str, Any]:
        """Override: extra fields for the heartbeat / dashboard."""
        return {}

    # ---- helpers --------------------------------------------------------------------------------------
    async def publish(self, topic: str, data: dict[str, Any]) -> str:
        return await self.bus.publish(topic, data)

    async def alert(self, text: str, level: str = "WARNING", key: str = "") -> None:
        self.log.log(getattr(logging, level, logging.WARNING), "%s", text)
        await self.bus.publish(topics.ALERT, {"level": level, "source": self.name, "text": text, "key": key,
                                              "ts": self.clock.time()})

    def spawn(self, coro: Awaitable[Any], name: str = "") -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro, name=name or None)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self._error(f"task {name or t.get_name()}", t.exception())

        task.add_done_callback(done)
        return task

    def _error(self, where: str, exc: BaseException) -> None:
        self.counters["errors"] += 1
        self.last_error = f"{where}: {type(exc).__name__}: {exc}"
        self.log.error("%s failed", where, exc_info=exc)

    async def _run_job(self, name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._error(f"job {name}", e)

    # ---- settings --------------------------------------------------------------------------------
    async def load_settings(self, wait_s: float = 0.0) -> None:
        """Take the config bot's current settings; fall back to the bootstrap file if it isn't up yet."""
        deadline = time.monotonic() + wait_s
        while True:
            cur = await self.bus.kv_get(topics.KV_CONFIG)
            if cur:
                try:
                    self.settings = parse_settings(cur["settings"])
                    self.settings_version = int(cur.get("version", 0))
                    return
                except SettingsError as e:
                    self.log.error("published settings are invalid (%s) - keeping previous", e)
                    return
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.2)

    async def _on_config_changed(self, msg: Message) -> None:
        old = self.settings
        await self.load_settings()
        if self.settings != old:
            await self.on_settings(old, self.settings)

    # ---- run loop ------------------------------------------------------------------------------------
    @property
    def subscribed_topics(self) -> list[str]:
        return sorted(set(self._handlers) | set(self._commands))

    async def run(self) -> None:
        self.started_at = time.time()
        await self.bus.ensure_group(self.name, self.subscribed_topics, self.start_from)
        await self.load_settings(wait_s=0.0)
        try:
            await self.setup()
        except Exception as e:
            self._error("setup", e)
            self.status = "failed"
            await self._heartbeat()
            raise
        self.status = "ok"
        loops = [asyncio.create_task(self._inbox(), name=f"{self.name}-inbox"),
                 asyncio.create_task(self._heartbeats(), name=f"{self.name}-hb")]
        loops += [asyncio.create_task(fn(), name=f"{self.name}-{n}") for n, fn in self._jobs]
        try:
            await self._stop.wait()
        finally:
            self.status = "stopping"
            for t in loops + list(self._tasks):
                t.cancel()
            await asyncio.gather(*loops, *self._tasks, return_exceptions=True)
            try:
                await self.teardown()
            except Exception as e:
                self._error("teardown", e)
            await self._heartbeat(final=True)

    def stop(self) -> None:
        self._stop.set()

    async def _inbox(self) -> None:
        tlist = self.subscribed_topics
        while not self._stop.is_set():
            try:
                msgs = await self.bus.consume(self.name, tlist, block_s=1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._error("bus consume", e)
                await asyncio.sleep(1.0)
                continue
            for m in msgs:
                await self._dispatch(m)
            if msgs:
                try:
                    await self.bus.ack(self.name, msgs)
                except Exception as e:
                    self._error("bus ack", e)

    async def _dispatch(self, m: Message) -> None:
        self.counters["messages"] += 1
        cmd = self._commands.get(m.topic)
        if cmd is not None:
            self.counters["commands"] += 1
            corr = m.data.pop("_corr", None)
            try:
                result = await cmd(m.data)
                reply = {"ok": True, **(result or {})}
            except Exception as e:
                self._error(f"command {m.topic}", e)
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if corr:
                await self.bus.send_reply(corr, reply)
            return
        for fn in self._handlers.get(m.topic, ()):
            try:
                await fn(m)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._error(f"handler {m.topic}", e)

    async def _heartbeats(self) -> None:
        n = 0
        while not self._stop.is_set():
            await self._heartbeat(publish_event=n % 6 == 0)
            n += 1
            await asyncio.sleep(self.heartbeat_s)

    async def _heartbeat(self, final: bool = False, publish_event: bool = True) -> None:
        try:
            details = self.details()
        except Exception as e:
            details = {"details_error": str(e)}
        hb = {"bot": self.name, "pid": os.getpid(), "status": "stopped" if final else self.status,
              "started_at": self.started_at, "ts": time.time(), "sim_time": self.clock.time(),
              "settings_version": self.settings_version, "counters": dict(self.counters),
              "last_error": self.last_error, "details": details}
        try:
            await self.bus.kv_set(topics.kv_heartbeat(self.name), hb, ttl=None if final else self.heartbeat_s * 4)
            if publish_event or final:
                await self.bus.publish(topics.BOT_HEARTBEAT, hb)
        except Exception as e:  # never let heartbeats kill the bot
            self.log.debug("heartbeat failed: %s", e)
