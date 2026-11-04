"""``bots``: set up, check, demo and run the team.

    bots init                     create config/settings.toml, config/dhanbot.toml, config/holidays.txt, .env
    bots set-password             set the dashboard password
    bots check                    read-only checks: settings, bus, Dhan token + data plan, AI key, dashboard
    bots demo                     the whole team on the simulated market (no account, no real money)
    bots run --all                every bot + the dashboard, supervised (what the server runs)
    bots run <bot>|dashboard      one bot in the foreground
    bots status                   health of every bot
    bots tables list|show|export  look at the tables
    bots ai test                  one tiny real Claude call to check the API key (a fraction of a cent)
    bots notify test|summary      send a test message (Telegram + email) or today's summary now
    bots config get|set|history|rollback

``--dir`` points at the project folder (default: current directory).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from datetime import timedelta
from pathlib import Path

from dhanbot.clock import epoch_at, ist_datetime

from .bots import BOT_NAMES
from .core import topics
from .core.bus import BusTimeout, make_bus
from .core.runtime import MarketCalendar, Paths
from .core.secrets import load_env
from .core.settings import Settings, SettingsError, bus_location, dashboard_address, deep_merge, diff, \
    load_settings_file, parse_settings, write_settings_file
from .runner import SimArgs, build_context, run_bot

TEMPLATES = Path(__file__).resolve().parent / "templates"
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"


def _p(*a) -> None:
    print(*a, flush=True)


def _settings(base: Path) -> Settings:
    try:
        return load_settings_file(base / "config" / "settings.toml")
    except SettingsError as e:
        _p(f"config/settings.toml is invalid:\n{e}")
        sys.exit(2)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _with_bus(base: Path, fn):
    s = _settings(base)
    paths = Paths(base, s)
    backend, redis_url = bus_location(s)
    bus = make_bus(backend, "cli", data_dir=paths.data, redis_url=redis_url)
    await bus.connect()
    try:
        return await fn(bus)
    finally:
        await bus.close()


def _write_env_value(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, done = [], False
    for ln in lines:
        if ln.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


# ---- init / password -------------------------------------------------------------------------------------------
def cmd_init(base: Path, force: bool = False) -> int:
    base.mkdir(parents=True, exist_ok=True)
    plan = [("settings.example.toml", base / "config" / "settings.toml"),
            ("dhanbot.example.toml", base / "config" / "dhanbot.toml"),
            ("holidays.txt", base / "config" / "holidays.txt"), ("env.example", base / ".env")]
    for src, dst in plan:
        if dst.exists() and not force:
            _p(f"  keep    {dst.relative_to(base)} (already there)")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATES / src, dst)
        _p(f"  created {dst.relative_to(base)}")
    try:
        os.chmod(base / ".env", 0o600)
    except OSError:
        pass
    _p("\nNext: `bots set-password`, then `bots demo` to try everything on the simulated market,\n"
       "or put your Dhan details in .env, set platform.name = \"dhan\" and run `bots check`.")
    return 0


def cmd_set_password(base: Path, password: str | None = None) -> int:
    from .dashboard.auth import hash_password

    if password is None:
        pw = getpass.getpass("New dashboard password: ")
        if len(pw) < 10:
            _p("Use at least 10 characters.")
            return 2
        if getpass.getpass("Repeat it: ") != pw:
            _p("The two entries differ.")
            return 2
    else:
        pw = password
    _write_env_value(base / ".env", "DASHBOARD_PASSWORD_HASH", hash_password(pw))
    _p("Saved (as a scrypt hash) in .env. Restart the dashboard to use it.")
    return 0


# ---- check --------------------------------------------------------------------------------------------------------
def cmd_check(base: Path, login: bool = False) -> int:
    s = _settings(base)
    env = load_env(base)
    ok = True

    def line(good: bool | None, what: str, detail: str = "") -> None:
        nonlocal ok
        mark = {True: "OK  ", False: "FAIL", None: "info"}[good]
        ok &= good is not False
        _p(f"[{mark}] {what}" + (f" - {detail}" if detail else ""))

    line(True, "settings", f"config/settings.toml valid (platform {s.platform.name})")

    async def bus_check(bus):
        await bus.kv_set("check:ping", {"t": time.time()}, ttl=10)
        return await bus.kv_get("check:ping")

    try:
        line(bool(asyncio.run(_with_bus(base, bus_check))), "message bus", bus_location(s)[0])
    except Exception as e:
        line(False, "message bus", f"{bus_location(s)[0]}: {e}")
    if s.platform.name == "dhan":
        ok_dhan = asyncio.run(_check_dhan(base, s, env, login, line))
        ok &= ok_dhan
    else:
        line(None, "Dhan", "platform is 'simulated' - no account needed")
    if s.execution.enabled:
        from dhanbot.config import load_config as load_dhan_config
        from dhanbot.errors import ConfigError

        path = Paths(base, s).resolve(s.execution.dhanbot_config)
        try:
            cfg = load_dhan_config(path, env={"DHAN_CLIENT_ID": env.get("DHAN_CLIENT_ID", "1"),
                                              "DHAN_ACCESS_TOKEN": "x"})
            live = s.execution.live and cfg.is_live
            line(True, "trading engine config", f"{path.name}: strategy {cfg.strategy.name}, "
                                                f"{'LIVE - real orders' if live else 'paper'}")
        except ConfigError as e:
            line(False, "trading engine config", "; ".join(e.problems))
    else:
        line(None, "trading engine", "execution.enabled = false (the team runs without trading)")
    if not s.ai.enabled:
        line(None, "AI analyst", "switched off")
    elif s.ai.provider == "stub":
        line(None, "AI analyst", "stub provider (rule-based demo reports)")
    else:
        key = env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")
        line(bool(key) or None, "AI analyst", f"{s.ai.model}, key {'present' if key else 'missing - the analyst stays off'}"
             " (run `bots ai test` to verify it)")
    line(bool(env.get("DASHBOARD_PASSWORD_HASH")), "dashboard password",
         "set" if env.get("DASHBOARD_PASSWORD_HASH") else "missing - run `bots set-password`")
    holidays = Paths(base, s).holidays_file
    line(holidays.exists() or None, "holiday calendar", str(holidays) if holidays.exists() else "config/holidays.txt "
         "missing - only weekends are treated as closed")
    _p("\nAll checks passed." if ok else "\nSome checks failed - see above.")
    return 0 if ok else 1


async def _check_dhan(base: Path, s: Settings, env: dict, login: bool, line) -> bool:
    from dhanbot.config import load_config as load_dhan_config
    from dhanbot.dhan.auth import TokenManager
    from dhanbot.dhan.endpoints import Endpoints
    from dhanbot.dhan.rest import DhanRest

    if not env.get("DHAN_CLIENT_ID"):
        line(False, "Dhan client id", "DHAN_CLIENT_ID missing in .env")
        return False
    paths = Paths(base, s)
    cfg = load_dhan_config(data={"dhan": {"auth": s.platform.dhan_auth}, "paths": {"data_dir": str(paths.bot("config"))}},
                           env=env)
    tm = TokenManager(cfg, Endpoints(), cache_path=paths.bot("config") / "dhan_token.json")
    try:
        token = await tm.ensure(allow_login=login and s.platform.dhan_auth == "totp")
    except Exception as e:
        line(False, "Dhan token", f"{e}" + ("" if login else " (add --login to log in with TOTP now)"))
        return False
    left = tm.seconds_left()
    line(True, "Dhan token", f"from {tm.source}, valid {left / 3600:.1f} h" if left != float("inf") else f"from {tm.source}")
    rest = DhanRest(Endpoints(), env["DHAN_CLIENT_ID"], token)
    await rest.start()
    try:
        prof = await rest.profile()
        if not prof.ok:
            line(False, "Dhan profile", str(prof.error))
            return False
        d = prof.data if isinstance(prof.data, dict) else {}
        plan = d.get("dataPlan") or d.get("data_plan")
        line(plan in ("Active", "active", True) or None, "Dhan data plan",
             f"{plan or 'unknown'} (valid till {d.get('dataValidity', '?')}) - live prices need an active plan")
        funds = await rest.get_funds()
        if funds.ok and isinstance(funds.data, dict):
            avail = funds.data.get("availabelBalance", funds.data.get("availableBalance"))
            line(True, "Dhan funds", f"available {avail}")
    finally:
        await rest.close()
    return True


# ---- run / demo ---------------------------------------------------------------------------------------------------
def _install_stop(stop) -> None:
    import signal

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop))


async def _supervise(base: Path, names: list[str], sim: SimArgs | None, dashboard: bool, child_env: dict | None = None,
                     minutes: float | None = None) -> int:
    from .core.runtime import setup_logging, shutdown_logging
    from .supervisor import SupervisorBot

    ctx = build_context("supervisor", base, sim)
    setup_logging(ctx.paths.logs, "supervisor", console=False)
    await ctx.bus.connect()
    sup = SupervisorBot(ctx, names, sim=sim, dashboard=dashboard, child_env=child_env)
    _install_stop(sup.stop)
    if minutes:
        asyncio.get_running_loop().call_later(minutes * 60, sup.stop)
    try:
        await sup.run()
    finally:
        await ctx.bus.close()
        shutdown_logging()
    return 0


def _team(s: Settings) -> list[str]:
    names = [n for n in BOT_NAMES if n != "sim_market"]
    return (["sim_market"] if s.platform.name == "simulated" else []) + names


def cmd_run(base: Path, target: str | None, all_: bool, no_dashboard: bool) -> int:
    s = _settings(base)
    if all_:
        sim = None
        if s.platform.name == "simulated":  # real-time simulated market on a fixed port, so restarts reconnect
            now = time.time()
            sim = SimArgs(now, s.platform.simulated_speed, now, _free_port())
        _p(f"Starting the team ({', '.join(_team(s))})" + ("" if no_dashboard else
           f" and the dashboard on http://{dashboard_address(s)}:{s.dashboard.port}") + ". Ctrl+C stops everything.")
        return asyncio.run(_supervise(base, _team(s), sim, not no_dashboard))
    if target == "dashboard":
        return run_dashboard(base, s)
    if target in BOT_NAMES:
        return asyncio.run(run_bot(target, base))
    _p(f"say `bots run --all`, `bots run dashboard` or one of: {', '.join(BOT_NAMES)}")
    return 2


def run_dashboard(base: Path, s: Settings, extra_env: dict | None = None) -> int:
    d = s.dashboard
    argv = [sys.executable, "-m", "streamlit", "run", str(DASHBOARD_DIR / "app.py"),
            "--server.address", dashboard_address(s), "--server.port", str(d.port),
            "--server.headless", "true", "--browser.gatherUsageStats", "false", "--", "--base", str(base)]
    return subprocess.call(argv, cwd=str(DASHBOARD_DIR), env={**os.environ, **(extra_env or {})})


DEMO_DHANBOT = """# dhan_bot configuration for `bots demo` (simulated exchange only)
[mode]
mode = "paper"
[session]
profile = "nse"
holidays_file = "holidays.txt"
[feed]
record_ticks = false
[risk]
max_daily_loss_inr = 5000
max_order_value_inr = 50000
max_gross_exposure_inr = 200000
max_open_positions = 3
max_consecutive_losses = 50
[perf]
gc_freeze = false
[strategy]
name = "tradingbots.demo_strategy:DemoRotation"
[strategy.params]
qty = 5
every_min = 15
hold_min = 10
"""


def _demo_instruments() -> str:
    out = ""
    for sym in ("RELIANCE", "HDFCBANK", "INFY"):
        out += (f'\n[[instruments]]\nalias = "{sym}"\nsegment = "NSE_EQ"\ntrading_symbol = "{sym}"\nmode = "full"\n'
                f'product = "INTRADAY"\nmax_qty = 20\n')
    return out


NOTIFY_ENV = ("TELEGRAM_", "SMTP_", "EMAIL_")


def cmd_demo(base: Path, speed: float, start_hhmm: str, use_ai: bool, no_dashboard: bool, minutes: float | None,
             reset: bool, port: int, password: str | None, notify: bool = False) -> int:
    project_env = load_env(Path.cwd())
    if reset and base.exists():
        shutil.rmtree(base)
    (base / "config").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATES / "holidays.txt", base / "config" / "holidays.txt")
    (base / "config" / "dhanbot.toml").write_text(DEMO_DHANBOT + _demo_instruments(), encoding="utf-8")
    ai_key = project_env.get("ANTHROPIC_API_KEY") if use_ai else None
    if use_ai and not ai_key:
        _p("--ai needs ANTHROPIC_API_KEY in .env (or the environment); running with demo stub reports instead.")
    demo = {"platform": {"name": "simulated"},
            "watchlist": [{"symbol": "NIFTY", "segment": "IDX_I", "security_id": 13, "mode": "ticker"},
                          {"symbol": "RELIANCE"}, {"symbol": "HDFCBANK"}, {"symbol": "INFY"}],
            "market_data": {"snapshot_interval_s": 60},
            "schedule": {"ai_runs": ["09:30", "12:30", "15:35"], "money_interval_s": 300},
            "ai": {"provider": "anthropic" if ai_key else "stub", "daily_usd_budget": 0.75},
            "budget": {"daily_capital_limit_inr": 120000, "max_capital_in_use_inr": 60000,
                       "per_stock_daily_limit_inr": 50000, "max_trades_per_day": 12, "max_trades_per_stock": 5,
                       "max_daily_loss_inr": 3000},
            "execution": {"enabled": True, "dhanbot_config": "config/dhanbot.toml"},
            "notify": {"enabled": notify, "quiet_hours": []},
            "dashboard": {"port": port, "refresh_s": 2.0}}
    write_settings_file(base / "config" / "settings.toml", parse_settings(demo))
    env_lines = []
    if ai_key:
        env_lines.append(f"ANTHROPIC_API_KEY={ai_key}")
    if notify:  # your Telegram / email from the project's .env, so the demo's alerts reach your phone
        env_lines += [f"{k}={v}" for k, v in project_env.items() if k.startswith(NOTIFY_ENV) and v]
    shown = password or project_env.get("DASHBOARD_DEMO_PASSWORD") or secrets.token_urlsafe(9)
    from .dashboard.auth import hash_password

    env_lines.append(f"DASHBOARD_PASSWORD_HASH={hash_password(shown)}")
    (base / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    try:
        os.chmod(base / ".env", 0o600)
    except OSError:
        pass
    cal = MarketCalendar.from_file(base / "config" / "holidays.txt")
    d = ist_datetime(time.time()).date() + timedelta(days=1)
    while not cal.is_trading_day(d):
        d += timedelta(days=1)
    start = epoch_at(d, start_hhmm)
    sim = SimArgs(start, speed, time.time(), _free_port())
    s = load_settings_file(base / "config" / "settings.toml")
    _p(f"Demo in {base}: simulated market, {d:%a %d %b} from {start_hhmm} IST at {speed:g}x speed "
       f"(a trading day takes about {375 / speed:.0f} minutes).")
    _p("The trading engine runs the DEMO strategy on the simulated exchange - no real money is involved.")
    _p("Notifications: " + ("on - alerts and the summary go to your Telegram / email from .env" if notify else
                            "off (add --notify to send the demo's alerts to the Telegram / email in your .env)"))
    _p("AI reports: " + ("Claude (real API calls, capped at $0.75 for the demo)" if ai_key else
                         "rule-based demo stub (run `bots demo --ai` with ANTHROPIC_API_KEY to use Claude)"))
    if not no_dashboard:
        _p(f"\nDashboard: http://127.0.0.1:{s.dashboard.port}   password: {shown}\n")
    _p("Ctrl+C stops the demo.")
    return asyncio.run(_supervise(base, _team(s), sim, not no_dashboard, {"TRADINGBOTS_DEMO_MANUAL": "1"}, minutes))


# ---- status / tables / ai / config -----------------------------------------------------------------------------
def cmd_status(base: Path) -> int:
    from .supervisor import KV_SUPERVISOR

    async def get(bus):
        return await bus.kv_get(KV_SUPERVISOR), {k[3:]: await bus.kv_get(k) for k in await bus.kv_keys("hb:")}

    sup, hbs = asyncio.run(_with_bus(base, get))
    now = time.time()
    rows = []
    if sup and now - float(sup.get("ts") or 0) < 30:
        for b in sup["bots"]:
            rows.append((b["name"], b["state"], f"{b['heartbeat_age_s']:.0f}s" if b.get("heartbeat_age_s") is not None
                         else "-", str(b.get("restarts", 0)), b.get("note") or b.get("last_error") or ""))
    else:
        if sup:
            _p("(the supervisor is not running - showing the bots' last heartbeats)")
        for n, hb in sorted(hbs.items()):
            age = now - float(hb.get("ts") or 0)
            rows.append((n, hb.get("status") if age < 30 else "silent", f"{age:.0f}s", "-", hb.get("last_error") or ""))
    if not rows:
        _p("No bots are running. Start them with `bots run --all` (or try `bots demo`).")
        return 1
    w = [max(len(r[i]) for r in rows + [("bot", "state", "heartbeat", "restarts", "note")]) for i in range(4)]
    for r in [("bot", "state", "heartbeat", "restarts", "note")] + rows:
        _p("  ".join(r[i].ljust(w[i]) for i in range(4)) + "  " + r[4][:80])
    return 0


def cmd_tables(base: Path, action: str, name: str | None, limit: int, out: str | None) -> int:
    import polars as pl

    from .core.schemas import TABLES
    from .core.tables_io import TableReader

    s = _settings(base)
    reader = TableReader(Paths(base, s).tables)
    if action == "list":
        have = reader.available()
        for n in [t for t in TABLES if t in have] + sorted(set(have) - set(TABLES)):
            _p(f"{n:<18} {reader.count(n):>9,} rows  {TABLES[n].description if n in TABLES else ''}")
        if not have:
            _p("No tables yet.")
        return 0
    if not name:
        _p("name a table (see `bots tables list`)")
        return 2
    df = reader.table(name, limit=None if action == "export" else limit)
    if action == "show":
        with pl.Config(tbl_rows=limit, tbl_cols=20, tbl_width_chars=200):
            _p(df)
        return 0
    path = Path(out or f"{name}.csv")
    df.write_csv(path)
    _p(f"wrote {df.height:,} rows to {path}")
    return 0


def cmd_ai_test(base: Path) -> int:
    s = _settings(base)

    async def via_bot(bus):
        hb = await bus.kv_get(topics.kv_heartbeat("ai_analyst"))
        if hb and time.time() - float(hb.get("ts") or 0) < 30:
            return await bus.request(topics.cmd("ai_analyst", "test"), {}, timeout=90)
        return None

    try:
        r = asyncio.run(_with_bus(base, via_bot))
    except BusTimeout:
        r = {"ok_key": False, "error": "the ai_analyst bot did not answer"}
    if r is None:  # the bot is not running: call directly (this call's cost is not added to the AI cost log)
        r = asyncio.run(_direct_ping(base, s))
    if r.get("ok_key"):
        _p(f"OK - {r.get('served_by')} replied {r.get('reply')!r} in {r.get('seconds')}s, cost ${r.get('cost_usd', 0):.6f}")
        return 0
    _p(f"FAILED - {r.get('error') or r.get('reason')}")
    return 1


async def _direct_ping(base: Path, s: Settings) -> dict:
    import anthropic

    from .bots.ai_analyst.claude import Claude, ClaudeError, Pong, ping_request

    env = load_env(base)
    kw = {k: v for k, v in (("api_key", env.get("ANTHROPIC_API_KEY")), ("auth_token", env.get("ANTHROPIC_AUTH_TOKEN")))
          if v}
    if not kw and not (Path.home() / ".config" / "anthropic").is_dir():
        return {"ok_key": False, "error": "no ANTHROPIC_API_KEY in .env"}
    claude = Claude(anthropic.AsyncAnthropic(max_retries=1, **kw), s.ai)
    try:
        res = await claude.run(ping_request(s.ai), Pong)
    except ClaudeError as e:
        return {"ok_key": e.kind not in ("auth", "permission"), "error": f"{e.kind}: {e}"}
    return {"ok_key": True, "reply": res.parsed.reply, "served_by": res.served_by, "cost_usd": res.usd,  # type: ignore
            "seconds": round(res.seconds, 2)}


def cmd_notify(base: Path, action: str) -> int:
    """Through the notifier bot when it runs (so its counters and settings apply), else straight from .env."""
    s = _settings(base)

    async def via_bot(bus):
        hb = await bus.kv_get(topics.kv_heartbeat("notifier"))
        if hb and time.time() - float(hb.get("ts") or 0) < 30:
            return await bus.request(topics.cmd("notifier", action), {}, timeout=45)
        return None

    try:
        r = asyncio.run(_with_bus(base, via_bot))
    except BusTimeout:
        _p("The notifier bot did not answer.")
        return 1
    if action == "summary":
        if r is None:
            _p("The daily summary is built from the running team: start it with `bots run --all` first.")
            return 1
        _p(r.get("text") or r)
        _p(f"\nsent to Telegram: {'yes' if r.get('telegram') else 'no'} · email: {'yes' if r.get('email') else 'no'}")
        return 0
    if r is None:
        r = asyncio.run(_direct_notify_test(base, s))
    for ch in ("telegram", "email"):
        _p(f"{ch:<9} {r.get(ch)}")
    return 0 if any(r.get(ch) == "sent" for ch in ("telegram", "email")) and \
        not any(str(r.get(ch)).startswith("failed") for ch in ("telegram", "email")) else 1


async def _direct_notify_test(base: Path, s: Settings) -> dict:
    from .bots.notifier.channels import open_channels, send_tests

    tg, em, why = open_channels(load_env(base), s.notify, {})
    return await send_tests(tg, em, why)


def _parse_value(raw: str):
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw  # a bare word is a string


def _nest(key: str, value) -> dict:
    out: dict = {}
    cur = out
    parts = key.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    return out


def cmd_config(base: Path, action: str, args: list[str]) -> int:
    import json

    async def ask(bus, action_: str, data: dict):
        try:
            return await bus.request(topics.cmd("config", action_), data, timeout=5)
        except BusTimeout:
            return None

    if action == "get":
        cur = asyncio.run(_with_bus(base, lambda b: b.kv_get(topics.KV_CONFIG)))
        data = cur["settings"] if cur else _settings(base).to_dict()
        for k in (args[0].split(".") if args else []):
            data = data[k]
        _p(json.dumps(data, indent=2, default=str))
        return 0
    if action == "set":
        patch: dict = {}
        for a in args:
            if "=" not in a:
                _p(f"expected key=value, got {a!r}")
                return 2
            k, v = a.split("=", 1)
            patch = deep_merge(patch, _nest(k.strip(), _parse_value(v.strip())))
        r = asyncio.run(_with_bus(base, lambda b: ask(b, "update", {"patch": patch, "author": "cli"})))
        if r is None:  # the config bot is not running: edit the file (it imports it on its next start)
            s = _settings(base)
            try:
                new = parse_settings(deep_merge(s.to_dict(), patch))
            except SettingsError as e:
                _p(f"not saved:\n{e}")
                return 2
            write_settings_file(base / "config" / "settings.toml", new)
            _p("saved to config/settings.toml (config bot not running):\n  " +
               "\n  ".join(diff(s.to_dict(), new.to_dict()) or ["no change"]))
            return 0
        if r.get("errors"):
            _p("not saved:\n  " + "\n  ".join(r["errors"]))
            return 2
        _p(f"saved as version {r.get('version')}:\n  " + "\n  ".join(r.get("changes") or ["no change"]))
        return 0
    if action in ("history", "rollback"):
        data = {"limit": 30} if action == "history" else {"version": int(args[0]) if args else 0}
        r = asyncio.run(_with_bus(base, lambda b: ask(b, action, data)))
        if r is None:
            _p("the config bot is not running (start it with `bots run --all`)")
            return 1
        if action == "history":
            for v in r.get("versions", []):
                _p(f"v{v['version']:<4} {time.strftime('%Y-%m-%d %H:%M', time.localtime(v['ts']))}  {v['author']:<10} "
                   f"{'; '.join(v.get('changes') or [])[:120]}")
        else:
            _p(f"restored as version {r.get('version')}" if r.get("applied") else f"not restored: {r.get('errors')}")
        return 0
    _p("config actions: get [key] | set key=value ... | history | rollback N")
    return 2


# ---- entry point -----------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bots", description="A team of cooperating trading bots around Dhan.")
    p.add_argument("--dir", default=".", help="project folder (default: current directory)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="create config files from templates")
    s.add_argument("--force", action="store_true", help="overwrite existing files")
    s = sub.add_parser("set-password", help="set the dashboard password")
    s.add_argument("--password", help=argparse.SUPPRESS)
    s = sub.add_parser("check", help="read-only checks")
    s.add_argument("--login", action="store_true", help="allow a TOTP login if no valid token is cached")
    s = sub.add_parser("demo", help="the whole team on the simulated market")
    s.add_argument("--speed", type=float, default=60.0, help="simulated clock speed (default 60x)")
    s.add_argument("--start", default="09:05", help="simulated start time, IST (default 09:05)")
    s.add_argument("--ai", action="store_true", help="use Claude (needs ANTHROPIC_API_KEY; real, capped cost)")
    s.add_argument("--no-dashboard", action="store_true")
    s.add_argument("--minutes", type=float, default=None, help="stop after this many real minutes")
    s.add_argument("--keep", action="store_true", help="keep the previous demo's data")
    s.add_argument("--port", type=int, default=8501)
    s.add_argument("--demo-dir", default=".demo", help="where the demo keeps its files (default .demo)")
    s.add_argument("--password", default=None, help="dashboard password for the demo (default: random)")
    s.add_argument("--notify", action="store_true", help="send the demo's alerts to your Telegram / email (.env)")
    s = sub.add_parser("run", help="run the team (--all) or one bot")
    s.add_argument("target", nargs="?", help="a bot name or 'dashboard'")
    s.add_argument("--all", action="store_true", help="every bot plus the dashboard, supervised")
    s.add_argument("--no-dashboard", action="store_true")
    sub.add_parser("status", help="health of every bot")
    s = sub.add_parser("tables", help="look at the tables")
    s.add_argument("action", choices=["list", "show", "export"])
    s.add_argument("name", nargs="?")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--out", default=None)
    s = sub.add_parser("ai", help="AI analyst tools")
    s.add_argument("action", choices=["test"])
    s = sub.add_parser("notify", help="Telegram and email notifications")
    s.add_argument("action", choices=["test", "summary"])
    s = sub.add_parser("config", help="settings from the command line")
    s.add_argument("action", choices=["get", "set", "history", "rollback"])
    s.add_argument("args", nargs="*")
    ns = p.parse_args(argv)
    base = Path(ns.dir).resolve()
    if ns.cmd == "init":
        code = cmd_init(base, ns.force)
    elif ns.cmd == "set-password":
        code = cmd_set_password(base, ns.password)
    elif ns.cmd == "check":
        code = cmd_check(base, ns.login)
    elif ns.cmd == "demo":
        code = cmd_demo((base / ns.demo_dir).resolve(), ns.speed, ns.start, ns.ai, ns.no_dashboard, ns.minutes,
                        not ns.keep, ns.port, ns.password, ns.notify)
    elif ns.cmd == "run":
        code = cmd_run(base, ns.target, ns.all, ns.no_dashboard)
    elif ns.cmd == "status":
        code = cmd_status(base)
    elif ns.cmd == "tables":
        code = cmd_tables(base, ns.action, ns.name, ns.limit, ns.out)
    elif ns.cmd == "ai":
        code = cmd_ai_test(base)
    elif ns.cmd == "notify":
        code = cmd_notify(base, ns.action)
    else:
        code = cmd_config(base, ns.action, ns.args)
    sys.exit(code)


if __name__ == "__main__":
    main()
