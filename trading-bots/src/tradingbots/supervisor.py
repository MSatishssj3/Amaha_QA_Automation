"""Supervisor: starts every bot as its own process, plus the dashboard, and keeps them running.

* Each bot runs as ``python -m tradingbots.runner <bot>``, so one crash never takes the others down.
* A bot that exits is restarted with backoff (1, 2, 4 ... 60 s). After 3 crashes in 10 minutes it is
  marked **degraded**, an alert is raised, and it is retried every 5 minutes.
* A bot whose heartbeat goes stale for 90 s while its process is still alive (hung) is restarted.
* Stopping is graceful and ordered:
  1. execution first, so it can flatten while market data is still up;
  2. then the other bots;
  3. config last.
  Each gets ``cmd.<bot>.shutdown`` (works on every OS), then a terminate, then a kill.
* ``cmd.supervisor.status`` / ``restart {bot}``. The status is also in the key-value store
  (``supervisor:status``) for ``bots status`` and the dashboard.
* Retained bus messages are trimmed every hour (``system.bus_retention_hours``).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .core import topics
from .core.bot import Bot
from .core.bus import Message
from .core.settings import dashboard_address
from .runner import SimArgs

KV_SUPERVISOR = "supervisor:status"
STOP_ORDER_FIRST = ("execution",)  # stopped first
STOP_ORDER_LAST = ("market_data", "sim_market", "config")  # stopped last, in this order
HEARTBEAT_STALE_S = 90.0
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
START_GRACE_S = 60.0


@dataclass
class Child:
    name: str
    argv: list[str]
    kind: str = "bot"  # bot | dashboard
    proc: asyncio.subprocess.Process | None = None
    state: str = "starting"  # starting | running | restarting | degraded | stopped
    restarts: int = 0
    crashes: deque = field(default_factory=lambda: deque(maxlen=10))
    started: float = 0.0
    last_exit: int | None = None
    note: str = ""
    task: asyncio.Task | None = None
    kill_requested: bool = False
    cwd: str = ""


class SupervisorBot(Bot):
    name = "supervisor"

    def __init__(self, ctx, names: list[str], *, sim: SimArgs | None = None, dashboard: bool = True,
                 python: str = sys.executable, child_env: dict[str, str] | None = None) -> None:
        self.names, self.sim, self.with_dashboard = list(names), sim, dashboard
        self.python, self.child_env = python, child_env or {}
        self.children: dict[str, Child] = {}
        self._stopping = False
        super().__init__(ctx)

    def configure(self) -> None:
        self.command("status", self.cmd_status)
        self.command("restart", self.cmd_restart)
        self.on(topics.ALERT, self.on_alert)

    def details(self) -> dict:
        return {"children": {n: c.state for n, c in self.children.items()}}

    # ---- children --------------------------------------------------------------------------------------------
    def _argv(self, name: str) -> list[str]:
        base = str(self.ctx.paths.base)
        if name == "dashboard":
            app = DASHBOARD_DIR / "app.py"
            d = self.settings.dashboard
            return [self.python, "-m", "streamlit", "run", str(app),
                    "--server.address", dashboard_address(self.settings), "--server.port", str(d.port),
                    "--server.headless", "true", "--browser.gatherUsageStats", "false", "--", "--base", base]
        return [self.python, "-m", "tradingbots.runner", name, "--base", base, "--quiet"] + \
            (self.sim.argv() if self.sim else [])

    async def setup(self) -> None:
        self.ctx.paths.logs.mkdir(parents=True, exist_ok=True)
        for n in self.names + (["dashboard"] if self.with_dashboard else []):
            c = Child(n, self._argv(n), kind="dashboard" if n == "dashboard" else "bot",
                      # Streamlit reads the dashboard's theme from .streamlit/ in its working directory
                      cwd=str(DASHBOARD_DIR if n == "dashboard" else self.ctx.paths.base))
            self.children[n] = c
            c.task = asyncio.get_running_loop().create_task(self._keep_running(c), name=f"child-{n}")
        self.spawn(self._watchdog(), name="watchdog")
        self.spawn(self._status_loop(), name="status")

    async def _start(self, c: Child) -> None:
        env = {**os.environ, **self.child_env, "PYTHONUNBUFFERED": "1"}
        err = open(self.ctx.paths.logs / f"{c.name}.stderr.log", "ab")  # tracebacks from before logging starts
        try:
            c.proc = await asyncio.create_subprocess_exec(*c.argv, stdout=asyncio.subprocess.DEVNULL, stderr=err,
                                                          env=env, cwd=c.cwd or str(self.ctx.paths.base))
        finally:
            err.close()
        c.started, c.state, c.note, c.kill_requested = time.time(), "running", "", False
        self.log.info("started %s (pid %s)", c.name, c.proc.pid)

    async def _keep_running(self, c: Child) -> None:
        while not self._stopping:
            try:
                await self._start(c)
            except Exception as e:
                c.state, c.note = "restarting", f"could not start: {e}"
                await self.alert(f"{c.name} could not be started: {e}", "ERROR", key=f"sup-{c.name}")
                await asyncio.sleep(30)
                continue
            assert c.proc is not None
            rc = await c.proc.wait()
            c.last_exit, c.proc = rc, None
            if self._stopping:
                c.state = "stopped"
                return
            now = time.monotonic()
            if not c.kill_requested:
                c.crashes.append(now)
            recent = [t for t in c.crashes if now - t <= 600]
            c.restarts += 1
            if len(recent) >= 3:
                c.state, delay = "degraded", 300.0
                c.note = f"crashed {len(recent)} times in 10 minutes (exit code {rc})"
                await self.alert(f"{c.name} is degraded: {c.note} - retrying every 5 minutes; see logs/{c.name}.jsonl "
                                 f"and logs/{c.name}.stderr.log", "ERROR", key=f"sup-{c.name}")
            else:
                c.state = "restarting"
                delay = 1.0 if c.kill_requested else min(60.0, 2.0 ** max(0, len(recent) - 1))
                c.note = f"exited with code {rc}; restarting in {delay:.0f}s"
                if not c.kill_requested:
                    await self.alert(f"{c.name} {c.note}", "WARNING", key=f"sup-{c.name}")
            await asyncio.sleep(delay)

    async def _status_loop(self) -> None:  # wall-clock timers, also under an accelerated demo clock
        last_trim = time.monotonic()
        while not self._stopping:
            await self.publish_status()
            if time.monotonic() - last_trim >= 3600:
                last_trim = time.monotonic()
                await self.trim()
            await asyncio.sleep(3.0)

    async def _watchdog(self) -> None:
        while not self._stopping:
            await asyncio.sleep(15)
            now = time.time()
            for c in self.children.values():
                if c.kind != "bot" or c.state != "running" or c.proc is None or now - c.started < START_GRACE_S:
                    continue
                hb = await self.bus.kv_get(topics.kv_heartbeat(c.name))
                age = now - float(hb["ts"]) if hb else None
                if age is None or age > HEARTBEAT_STALE_S:
                    await self.alert(f"{c.name} stopped sending heartbeats ({'none' if age is None else f'{age:.0f}s'}) "
                                     "- restarting it", "WARNING", key=f"sup-{c.name}")
                    await self._kill(c)

    async def _kill(self, c: Child, grace: float = 20.0) -> None:
        p = c.proc
        if p is None or p.returncode is not None:
            return
        c.kill_requested = True
        if c.kind == "bot":
            try:
                await self.bus.publish(topics.cmd(c.name, "shutdown"), {"ts": time.time()})
                await asyncio.wait_for(p.wait(), grace)
                return
            except (asyncio.TimeoutError, Exception):
                pass
        try:
            p.terminate()
            await asyncio.wait_for(p.wait(), 10)
        except asyncio.TimeoutError:
            p.kill()
        except ProcessLookupError:
            pass

    async def teardown(self) -> None:
        self._stopping = True
        order = [n for n in STOP_ORDER_FIRST if n in self.children]
        order += [n for n in self.children if n not in STOP_ORDER_FIRST and n not in STOP_ORDER_LAST]
        order += [n for n in STOP_ORDER_LAST if n in self.children]
        for n in order:  # execution may need ~30 s to flatten on shutdown
            await self._kill(self.children[n], grace=45.0 if n == "execution" else 20.0)
        tasks = [c.task for c in self.children.values() if c.task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.publish_status()

    # ---- status / commands -------------------------------------------------------------------------------------
    async def status_rows(self) -> list[dict]:
        rows, now = [], time.time()
        for c in self.children.values():
            hb = await self.bus.kv_get(topics.kv_heartbeat(c.name)) if c.kind == "bot" else None
            rows.append({"name": c.name, "kind": c.kind, "state": c.state, "pid": c.proc.pid if c.proc else None,
                         "uptime_s": round(now - c.started) if c.proc else 0, "restarts": c.restarts,
                         "last_exit": c.last_exit, "note": c.note,
                         "heartbeat_age_s": round(now - float(hb["ts"]), 1) if hb else None,
                         "bot_status": (hb or {}).get("status"), "last_error": (hb or {}).get("last_error", ""),
                         "errors": ((hb or {}).get("counters") or {}).get("errors", 0)})
        return rows

    async def publish_status(self) -> None:
        await self.bus.kv_set(KV_SUPERVISOR, {"ts": time.time(), "pid": os.getpid(), "stopping": self._stopping,
                                              "sim": self.sim.__dict__ if self.sim else None,
                                              "bots": await self.status_rows()})

    async def cmd_status(self, data: dict) -> dict:
        return {"bots": await self.status_rows()}

    async def cmd_restart(self, data: dict) -> dict:
        c = self.children.get(str(data.get("bot") or ""))
        if c is None:
            return {"restarted": False, "error": f"unknown bot; one of {', '.join(self.children)}"}
        self.spawn(self._kill(c), name=f"restart-{c.name}")
        return {"restarted": True, "bot": c.name}

    async def trim(self) -> None:
        await self.bus.trim(self.settings.system.bus_retention_hours * 3600)

    async def on_alert(self, msg: Message) -> None:
        a = msg.data
        if a.get("level") in ("WARNING", "ERROR") or str(a.get("key", "")).startswith(("sup-", "exec-started")):
            print(f"[{a.get('level', 'INFO')}] {a.get('source')}: {a.get('text')}", flush=True)
