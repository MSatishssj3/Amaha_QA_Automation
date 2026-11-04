"""Run one bot in this process.

    python -m tradingbots.runner <bot> --base DIR [--quiet] [--sim-start EPOCH --sim-speed X --sim-ref WALL]
                                                  [--sim-port PORT]

``bots run <bot>`` and the supervisor both use this. A simulated session passes the same
``--sim-*`` values to every process so that all bots share one accelerated clock.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from dhanbot.clock import RealClock

from .bots import BOT_NAMES, bot_class
from .core.bus import make_bus
from .core.runtime import BotContext, MarketCalendar, Paths, SharedSimClock, setup_logging, shutdown_logging
from .core.secrets import SECRET_KEYS, load_env
from .core.settings import Settings, SettingsError, load_settings_file


@dataclass
class SimArgs:
    start: float
    speed: float
    wall_ref: float
    port: int = 0

    def argv(self) -> list[str]:
        out = ["--sim-start", repr(self.start), "--sim-speed", repr(self.speed), "--sim-ref", repr(self.wall_ref)]
        return out + (["--sim-port", str(self.port)] if self.port else [])


def load_bootstrap(base: Path) -> Settings:
    return load_settings_file(base / "config" / "settings.toml")


def build_context(name: str, base_dir: Path, sim: SimArgs | None = None, settings: Settings | None = None) -> BotContext:
    base = Path(base_dir).resolve()
    bootstrap = settings or load_bootstrap(base)
    paths = Paths(base, bootstrap)
    clock = SharedSimClock(sim.start, sim.speed, sim.wall_ref) if sim else RealClock()
    calendar = MarketCalendar.from_file(paths.holidays_file) if paths.holidays_file.exists() else MarketCalendar()
    bus = make_bus(bootstrap.system.bus_backend, name, data_dir=paths.data, redis_url=bootstrap.system.redis_url)
    return BotContext(bus=bus, paths=paths, clock=clock, bootstrap=bootstrap, calendar=calendar,
                      sim={"port": sim.port} if sim and sim.port else None)


def _install_signals(stop) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError):  # Windows: plain handlers, hand over to the loop
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop))


async def run_bot(name: str, base_dir: Path, sim: SimArgs | None = None, console: bool = True) -> int:
    if name not in BOT_NAMES:
        print(f"unknown bot {name!r}; one of: {', '.join(BOT_NAMES)}", file=sys.stderr)
        return 2
    base = Path(base_dir).resolve()
    try:
        ctx = build_context(name, base, sim)
    except SettingsError as e:
        print(f"config/settings.toml is invalid:\n{e}", file=sys.stderr)
        return 2
    env = load_env(base)  # registers secrets, so logs never contain them
    ctx.paths.data.mkdir(parents=True, exist_ok=True)
    setup_logging(ctx.paths.logs, name, console=console, secrets=[env[k] for k in SECRET_KEYS if env.get(k)])
    log = logging.getLogger(f"runner.{name}")
    await ctx.bus.connect()
    bot = bot_class(name)(ctx)
    _install_signals(bot.stop)
    try:
        await bot.run()
        return 0
    except Exception:
        log.exception("%s stopped with an error", name)
        return 1
    finally:
        await ctx.bus.close()
        shutdown_logging()


def parse_sim(ns: argparse.Namespace) -> SimArgs | None:
    if ns.sim_start is None:
        return None
    return SimArgs(float(ns.sim_start), float(ns.sim_speed or 1.0), float(ns.sim_ref), int(ns.sim_port or 0))


def add_sim_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sim-start", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--sim-speed", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--sim-ref", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--sim-port", type=int, default=0, help=argparse.SUPPRESS)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m tradingbots.runner")
    p.add_argument("bot", choices=BOT_NAMES)
    p.add_argument("--base", default=".", help="project folder (contains config/ and .env)")
    p.add_argument("--quiet", action="store_true", help="log to logs/<bot>.jsonl only, not the console")
    add_sim_args(p)
    ns = p.parse_args(argv)
    sys.exit(asyncio.run(run_bot(ns.bot, Path(ns.base), parse_sim(ns), console=not ns.quiet)))


if __name__ == "__main__":
    main()
