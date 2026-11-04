"""``dhanbot`` command line."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sqlite3
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Coroutine

import orjson

from . import __version__
from .config import AppConfig, load_config
from .errors import ConfigError, PreflightError
from .logging_setup import register_secrets, setup_logging, shutdown_logging

log = logging.getLogger("dhanbot")

EXIT_OK, EXIT_CONFIG, EXIT_PREFLIGHT, EXIT_RUNTIME = 0, 1, 2, 3
PKG_ROOT = Path(__file__).resolve().parents[2]


def run_async(coro: Coroutine, use_uvloop: bool = True) -> Any:
    if use_uvloop and sys.platform != "win32":
        try:
            import uvloop

            if sys.version_info >= (3, 12):
                return asyncio.run(coro, loop_factory=uvloop.new_event_loop)
            uvloop.install()
        except ImportError:
            pass
    return asyncio.run(coro)


def _load(args: argparse.Namespace, live: bool = False) -> AppConfig:
    path = Path(args.config)
    if not path.exists():
        raise ConfigError([f"{path} not found - run `dhanbot init` to create config.toml and .env"])
    cfg = load_config(path, live_requested=live)
    register_secrets(cfg.secrets.values())
    return cfg


def _logging(cfg: AppConfig | None, level: str, to_file: bool = True) -> None:
    setup_logging(cfg.log_dir if (cfg is not None and to_file) else None, level)


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir)
    target.mkdir(parents=True, exist_ok=True)
    made = []
    for src, dst in (("config.example.toml", "config.toml"), (".env.example", ".env")):
        s = PKG_ROOT / src
        d = target / dst
        if d.exists():
            print(f"  {d} exists - left unchanged")
            continue
        if not s.exists():
            print(f"  template {s} not found (installed without the source tree?)")
            continue
        shutil.copyfile(s, d)
        if dst == ".env":
            try:
                d.chmod(0o600)
            except OSError:
                pass
        made.append(str(d))
    hol = target / "holidays.txt"
    if not hol.exists() and (PKG_ROOT / "holidays.txt").exists():
        shutil.copyfile(PKG_ROOT / "holidays.txt", hol)
        made.append(str(hol))
    print("created: " + (", ".join(made) if made else "nothing"))
    print("next: edit .env (DHAN_CLIENT_ID + token or PIN/TOTP), review config.toml, then run `dhanbot check`")
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    from .checks import format_rows, run_check

    cfg = _load(args)
    _logging(cfg, "WARNING", to_file=False)
    rows = run_async(run_check(cfg, feed_seconds=args.feed_seconds), cfg.perf.uvloop)
    print(format_rows(rows))
    fails = [r for r in rows if r.status == "FAIL"]
    print(f"\n{'NOT READY' if fails else 'READY'}: {len(fails)} failure(s), "
          f"{sum(r.status == 'WARN' for r in rows)} warning(s)")
    return EXIT_PREFLIGHT if fails else EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    from .dhan.auth import TokenManager

    cfg = _load(args)
    _logging(cfg, "INFO", to_file=False)
    tm = TokenManager(cfg, cfg.endpoints.build())

    async def go() -> None:
        if args.token:
            tm.save_manual(args.token)
        elif args.oauth:
            consent, url = await tm.oauth_consent()
            print(f"1) open this URL and log in:\n   {url}\n2) after login you are redirected to a URL "
                  f"containing tokenId=...; paste that tokenId here")
            token_id = input("tokenId: ").strip()
            await tm.oauth_consume(token_id)
        else:
            await tm.login_totp()
        print(f"token saved to {tm.cache_path} (valid {tm.seconds_left() / 3600:.1f} h)")

    run_async(go(), cfg.perf.uvloop)
    return EXIT_OK


def cmd_ip(args: argparse.Namespace) -> int:
    from .dhan.auth import TokenManager
    from .dhan.rest import DhanRest

    cfg = _load(args)
    _logging(cfg, "WARNING", to_file=False)

    async def go() -> int:
        ep = cfg.endpoints.build()
        tm = TokenManager(cfg, ep)
        token = await tm.ensure(allow_login=cfg.dhan.auth == "totp")
        rest = DhanRest(ep, cfg.secrets.client_id, token)
        await rest.start()
        try:
            if args.action == "show":
                res = await rest.get_ip()
            else:
                if not args.address:
                    print("give the IP address, e.g. dhanbot ip set 203.0.113.10")
                    return EXIT_CONFIG
                print("note: Dhan locks a static IP for 7 days after it is set or changed")
                flag = "SECONDARY" if args.secondary else "PRIMARY"
                res = await (rest.set_ip if args.action == "set" else rest.modify_ip)(args.address, flag)
            print(orjson.dumps(res.data if res.ok else {"error": str(res.error)}, option=orjson.OPT_INDENT_2).decode())
            return EXIT_OK if res.ok else EXIT_RUNTIME
        finally:
            await rest.close()

    return run_async(go(), cfg.perf.uvloop)


def cmd_ping(args: argparse.Namespace) -> int:
    from urllib.parse import urlparse

    from .dhan.auth import TokenManager
    from .dhan.rest import DhanRest

    cfg = _load(args)
    _logging(cfg, "WARNING", to_file=False)

    async def go() -> int:
        ep = cfg.endpoints.build()
        u = urlparse(ep.rest_base)
        port = u.port or (443 if u.scheme == "https" else 80)
        tcp = []
        for _ in range(min(args.count, 10)):
            t0 = time.perf_counter()
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, port), 5)
                tcp.append((time.perf_counter() - t0) * 1000)
                w.close()
            except Exception as e:
                print(f"TCP connect to {u.hostname}:{port} failed: {e}")
                return EXIT_RUNTIME
        tm = TokenManager(cfg, ep)
        token = await tm.ensure(allow_login=cfg.dhan.auth == "totp")
        rest = DhanRest(ep, cfg.secrets.client_id, token)
        await rest.start()
        try:
            first = await rest.profile()
            https = []
            for _ in range(args.count):
                res = await rest.profile()
                if res.ok:
                    https.append(res.rtt_ns / 1e6)
                await asyncio.sleep(0.06)  # stay well inside the non-trading rate limit
        finally:
            await rest.close()

        def fmt(xs: list[float]) -> str:
            if not xs:
                return "no samples"
            xs = sorted(xs)
            return (f"min {xs[0]:.2f}  p50 {statistics.median(xs):.2f}  p95 {xs[int(0.95 * (len(xs) - 1))]:.2f}  "
                    f"max {xs[-1]:.2f} ms ({len(xs)} samples)")

        print(f"host {u.hostname}")
        print(f"  TCP connect        {fmt(tcp)}")
        print(f"  HTTPS (first call) {first.rtt_ns / 1e6:.2f} ms (includes TLS handshake)")
        print(f"  HTTPS keep-alive   {fmt(https)}   <- what each order pays in network time")
        if https and statistics.median(https) > 15:
            print("  tip: >15 ms usually means you are far from Mumbai; a VPS in AWS ap-south-1 is typically 1-5 ms")
        return EXIT_OK

    return run_async(go(), cfg.perf.uvloop)


def cmd_instruments(args: argparse.Namespace) -> int:
    from .instruments import InstrumentStore

    cfg = _load(args)
    _logging(cfg, "WARNING", to_file=False)

    async def get_store() -> InstrumentStore:
        if args.csv:
            return InstrumentStore.from_csv_text(Path(args.csv).read_text(encoding="utf-8"),
                                                 tick_unit=cfg.instrument_master.tick_unit,
                                                 freeze_qty=cfg.freeze_qty)
        segs = {"NSE_EQ", "NSE_FNO", "BSE_EQ", "BSE_FNO", "IDX_I", "MCX_COMM"}
        return await InstrumentStore.load(cfg, cfg.endpoints.build().instruments_csv_url, segs, date.today())

    store = run_async(get_store(), cfg.perf.uvloop)
    if args.action == "search":
        for i in store.search(" ".join(args.text), limit=args.limit, segment=args.segment):
            extra = f" exp={i.expiry} strike={i.strike_paise / 100:g} {i.option_type}" if i.expiry else ""
            print(f"{i.segment:<9} {i.security_id:>8}  {i.trading_symbol:<32} lot={i.lot_size:<5} "
                  f"tick={i.tick_paise / 100:.2f}{extra}")
    else:
        und = " ".join(args.text).upper()
        print("expiries:", ", ".join(str(e) for e in store.expiries(und, args.kind)[:8]))
        if args.spot is None:
            print("pass --spot <price> to resolve a strike (ATM etc.)")
            return EXIT_OK
        expiry: Any = date.fromisoformat(args.expiry) if args.expiry[:2] == "20" else args.expiry
        strike: Any = float(args.strike) if args.strike.replace(".", "").isdigit() else args.strike
        i = store.option(und, args.kind, args.spot, expiry=expiry, strike=strike)
        print(f"{i.segment} {i.security_id} {i.trading_symbol} lot={i.lot_size} expiry={i.expiry} "
              f"strike={i.strike_paise / 100:g} {i.option_type}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    from .clock import is_trading_day
    from .engine import Engine

    cfg = _load(args, live=args.live)
    _logging(cfg, args.log_level)
    if cfg.is_live:
        log.warning("LIVE MODE - real orders will be placed (max daily loss ₹%s)", cfg.risk.max_daily_loss_inr)
    engine = Engine(cfg)
    today = engine.clock.today()
    if cfg.session.exit_on_holiday and not is_trading_day(today, engine.holidays) and not args.force:
        log.info("%s is not a trading day (weekend/holiday) - exiting. Use --force to run anyway.", today)
        return EXIT_OK
    try:
        run_async(engine.run(), cfg.perf.uvloop)
    except PreflightError as e:
        log.error("%s", e)
        return EXIT_PREFLIGHT
    except Exception:
        log.exception("fatal error")
        return EXIT_RUNTIME
    finally:
        shutdown_logging()
    return EXIT_OK


def _next_trading_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def cmd_simulate(args: argparse.Namespace) -> int:
    """Full bot (LIVE code path) against the local mock Dhan, with an accelerated clock."""
    import dataclasses

    from .clock import SimClock, epoch_at
    from .config import EndpointsCfg, ModeCfg, PathsCfg, Secrets
    from .engine import Engine
    from .sim.mock_dhan import MockDhan

    base = _load(args)
    _logging(None, args.log_level)

    async def go() -> int:
        from .clock import is_trading_day, load_holidays

        holidays = load_holidays(base.resolve_path(base.session.holidays_file))
        day = date.today()
        while not is_trading_day(day, holidays):
            day += timedelta(days=1)
        clock = SimClock(epoch_at(day, args.start), speed=args.speed)
        mock = MockDhan(clock=clock, seed=args.seed)
        mock.ips["primaryIP"] = "127.0.0.1"
        ep = await mock.start()
        sim_dir = base.data_dir / "simulate"
        cfg = dataclasses.replace(
            base, mode=ModeCfg("live"),
            endpoints=EndpointsCfg(ep.rest_base, ep.auth_base, ep.feed_url, ep.order_ws_url, ep.instruments_csv_url),
            secrets=Secrets(client_id=mock.client_id, access_token=mock.token),
            paths=PathsCfg(str(sim_dir), str(sim_dir / "logs")),
            dhan=dataclasses.replace(base.dhan, auth="token", expected_ip=""),
            telegram=dataclasses.replace(base.telegram, enabled=False))
        engine = Engine(cfg, clock=clock, endpoints=ep, use_telegram=False)
        print(f"simulating {day} from {args.start} IST at {args.speed:g}x against a local mock Dhan "
              f"(data in {sim_dir}); Ctrl+C to stop")
        try:
            await engine.start()
            end = clock.time() + args.minutes * 60 if args.minutes else engine.session.today(clock.time())["session_end"]
            while clock.time() < end and not engine.stop_reason:
                await asyncio.sleep(0.2)
            engine.request_stop("simulation finished")
            report = await engine.shutdown()
        except KeyboardInterrupt:
            report = await engine.shutdown()
        finally:
            await mock.stop()
        print(Engine.report_text(report))
        from .latency import StageStats, format_stats

        print(format_stats([StageStats(**{k: v for k, v in s.items()}) for s in report["latency"]]))
        return EXIT_OK

    try:
        return run_async(go(), base.perf.uvloop)
    finally:
        shutdown_logging()


def cmd_replay(args: argparse.Namespace) -> int:
    from .instruments import InstrumentStore
    from .recorder import iter_frames
    from .replay import run_replay, synth_frames, synth_instrument_store

    cfg = _load(args)
    _logging(None, args.log_level)

    async def go() -> dict:
        if args.synthetic or not args.file:
            day = date.fromisoformat(args.date) if args.date else _next_trading_day(date.today())
            frames = synth_frames(args.symbols.split(","), day, minutes=args.minutes, seed=args.seed, step_s=1.0)
            store = synth_instrument_store(day)
        else:
            path = Path(args.file)
            frames = iter_frames(path)
            csvs = sorted((cfg.data_dir / "instruments").glob("????-??-??.csv"))
            if args.csv:
                csvs = [Path(args.csv)]
            if not csvs:
                raise SystemExit("no cached instrument master in data/instruments - run `dhanbot check` once "
                                 "or pass --csv")
            segs = {i.segment for i in cfg.instruments} | {"IDX_I"}
            store = InstrumentStore.from_csv_text(csvs[-1].read_text(encoding="utf-8"), segs,
                                                  cfg.instrument_master.tick_unit, cfg.freeze_qty)
        import dataclasses

        from .config import PathsCfg

        rcfg = dataclasses.replace(cfg, paths=PathsCfg(str(cfg.data_dir / "replay"), str(cfg.log_dir)))
        return await run_replay(rcfg, frames, instruments=store, speed=args.speed)

    from .engine import Engine

    report = run_async(go(), cfg.perf.uvloop)
    print(Engine.report_text(report))
    if args.json:
        print(orjson.dumps(report, option=orjson.OPT_INDENT_2, default=str).decode())
    shutdown_logging()
    return EXIT_OK


def cmd_synth(args: argparse.Namespace) -> int:
    from .recorder import write_frames
    from .replay import synth_frames

    day = date.fromisoformat(args.date) if args.date else _next_trading_day(date.today())
    frames = synth_frames(args.symbols.split(","), day, minutes=args.minutes, seed=args.seed, step_s=args.step)
    write_frames(Path(args.out), frames)
    print(f"wrote {len(frames)} frames for {day} to {args.out}")
    return EXIT_OK


def cmd_control(args: argparse.Namespace) -> int:
    from .control import send_command

    cfg = _load(args)
    path = cfg.data_dir / "control.json"
    resp = asyncio.run(send_command(path, args.command, timeout=120))
    if not resp.get("ok") and args.command == "killswitch":
        kill = cfg.data_dir / "KILL"
        kill.parent.mkdir(parents=True, exist_ok=True)
        kill.write_text(f"requested {time.ctime()}\n")
        print(f"bot not reachable ({resp.get('error')}); created {kill} - a running bot halts within 1 second")
        return EXIT_OK
    print(orjson.dumps(resp, option=orjson.OPT_INDENT_2, default=str).decode())
    return EXIT_OK if resp.get("ok") else EXIT_RUNTIME


def cmd_report(args: argparse.Namespace) -> int:
    cfg = _load(args)
    db = cfg.data_dir / "journal.db"
    if not db.exists():
        print(f"no journal at {db}")
        return EXIT_RUNTIME
    from datetime import datetime

    from .clock import IST

    day = date.fromisoformat(args.date) if args.date else datetime.now(IST).date()
    start = datetime(day.year, day.month, day.day, tzinfo=IST).timestamp()
    end = start + 86400
    con = sqlite3.connect(db)
    try:
        orders = con.execute("SELECT cid, symbol, side, qty, order_type, kind, role, status, filled_qty, avg_price, "
                             "reason FROM orders WHERE created >= ? AND created < ? ORDER BY created", (start, end)).fetchall()
        fills = con.execute("SELECT symbol, side, qty, price, leg, ts FROM fills WHERE ts >= ? AND ts < ? ORDER BY ts",
                            (start, end)).fetchall()
        pnl = con.execute("SELECT net, realised, unrealised, charges FROM pnl WHERE ts >= ? AND ts < ? ORDER BY ts "
                          "DESC LIMIT 1", (start, end)).fetchone()
        lat = con.execute("SELECT stage, SUM(n), AVG(p50_us), MAX(p99_us) FROM latency WHERE ts >= ? AND ts < ? "
                          "GROUP BY stage", (start, end)).fetchall()
    finally:
        con.close()
    print(f"== {day}: {len(orders)} orders, {len(fills)} fills")
    for o in orders:
        print(f"  {o[0]} {o[2]:<4} {o[3]:>5} {o[1]:<24} {o[4]:<16} {o[5]:<7} {o[6]:<9} {o[7]:<11} "
              f"filled={o[8]} avg={o[9]} {o[10] or ''}")
    for f in fills:
        print(f"  fill {f[1]:<4} {f[2]:>5} {f[0]:<24} @ {f[3]:.2f} {f[4]}")
    if pnl:
        print(f"P&L (last snapshot): net ₹{pnl[0]:,.2f} realised {pnl[1]:,.2f} unrealised {pnl[2]:,.2f} "
              f"charges {pnl[3]:,.2f}")
    for s in lat:
        print(f"latency {s[0]:<20} n={s[1]:<7} p50≈{s[2]:.1f}us p99max={s[3]:.1f}us")
    reports = sorted((cfg.data_dir / "reports").glob(f"{day.isoformat()}-*.json"))
    for r in reports:
        d = orjson.loads(r.read_bytes())
        print(f"run report {r.name}: net ₹{d['pnl']['net']:,.2f}, state {d['state']} ({d['state_reason']})")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dhanbot", description="Low-latency trading framework for Dhan")
    p.add_argument("--version", action="version", version=f"dhanbot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        if name not in ("init", "synth"):
            sp.add_argument("--config", "-c", default="config.toml", help="config file (default: config.toml)")
        return sp

    sp = add("init", cmd_init, "create config.toml, .env and holidays.txt from the templates")
    sp.add_argument("dir", nargs="?", default=".")
    sp = add("check", cmd_check, "read-only readiness check (token, data plan, static IP, instruments, feed)")
    sp.add_argument("--feed-seconds", type=float, default=10.0)
    sp = add("login", cmd_login, "generate and cache today's access token")
    sp.add_argument("--oauth", action="store_true", help="interactive OAuth flow (DHAN_APP_ID/SECRET)")
    sp.add_argument("--token", help="save a token pasted from web.dhan.co")
    sp = add("ip", cmd_ip, "show / set / modify the static IP registered with Dhan")
    sp.add_argument("action", choices=["show", "set", "modify"])
    sp.add_argument("address", nargs="?")
    sp.add_argument("--secondary", action="store_true")
    sp = add("ping", cmd_ping, "measure network latency to the Dhan API from this machine")
    sp.add_argument("-n", "--count", type=int, default=20)
    sp = add("instruments", cmd_instruments, "search instruments / resolve option contracts")
    sp.add_argument("action", choices=["search", "option"])
    sp.add_argument("text", nargs="+")
    sp.add_argument("--segment")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--kind", default="CE", choices=["CE", "PE"])
    sp.add_argument("--expiry", default="nearest")
    sp.add_argument("--strike", default="ATM")
    sp.add_argument("--spot", type=float)
    sp.add_argument("--csv", help="use a local scrip-master CSV instead of downloading")
    sp = add("run", cmd_run, "run the trading session (paper by default)")
    sp.add_argument("--live", action="store_true", help="required (together with mode='live') for real orders")
    sp.add_argument("--force", action="store_true", help="run even on a weekend/holiday")
    sp.add_argument("--log-level", default="INFO")
    sp = add("simulate", cmd_simulate, "run the whole bot against a local mock Dhan (no account needed)")
    sp.add_argument("--speed", type=float, default=60.0, help="clock speed-up factor (default 60x)")
    sp.add_argument("--minutes", type=float, default=0, help="simulated minutes to run (default: until session end)")
    sp.add_argument("--start", default="09:14:30", help="simulated start time IST")
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--log-level", default="INFO")
    sp = add("replay", cmd_replay, "backtest the strategy on a recorded (or synthetic) tick file")
    sp.add_argument("file", nargs="?")
    sp.add_argument("--synthetic", action="store_true")
    sp.add_argument("--symbols", default="RELIANCE,NIFTY")
    sp.add_argument("--minutes", type=int, default=375)
    sp.add_argument("--date")
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--speed", type=float, default=None, help="pace at N x real time (default: max speed)")
    sp.add_argument("--csv", help="scrip-master CSV matching the recording")
    sp.add_argument("--json", action="store_true", help="print the full JSON report")
    sp.add_argument("--log-level", default="WARNING")
    sp = add("synth", cmd_synth, "write a synthetic tick recording (for replay demos/tests)")
    sp.add_argument("out")
    sp.add_argument("--symbols", default="RELIANCE,NIFTY")
    sp.add_argument("--minutes", type=int, default=375)
    sp.add_argument("--step", type=float, default=1.0)
    sp.add_argument("--date")
    sp.add_argument("--seed", type=int, default=7)
    for name, help_ in (("status", "show the running bot's state"), ("pause", "stop new entries"),
                        ("resume", "allow new entries again"), ("flatten", "exit all bot positions (pauses entries)"),
                        ("killswitch", "HALT for the day and flatten (creates data/KILL if the bot is unreachable)"),
                        ("stop", "graceful shutdown of the running bot")):
        sp = add(name, cmd_control, help_)
        sp.set_defaults(command=name)
    add("report", cmd_report, "print a day's orders, fills, P&L and latency from the journal").add_argument("--date")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        shutdown_logging()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
