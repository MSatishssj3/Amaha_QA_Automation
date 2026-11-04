"""``dhanbot check``: a read-only readiness check. It never places, modifies or cancels orders and
never changes account settings.

It also verifies the items that could not be confirmed while building the bot offline (LTT encoding,
instrument tick sizes, order-update socket login) - see README "Verify on first live run".
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from .clock import LttNormalizer, RealClock, is_trading_day, load_holidays
from .config import AppConfig
from .dhan import packets as P
from .dhan.auth import AuthError, TokenManager
from .dhan.endpoints import MODE_CODES, Endpoints
from .dhan.feed import FeedConnection
from .dhan.normalize import available_balance
from .dhan.order_ws import OrderUpdateConnection
from .dhan.rest import DhanRest
from .instruments import InstrumentError, InstrumentStore
from .strategy.loader import StrategyLoadError, load_strategy

log = logging.getLogger(__name__)


@dataclass
class Row:
    name: str
    status: str  # OK | WARN | FAIL | INFO
    detail: str


async def run_check(cfg: AppConfig, endpoints: Endpoints | None = None, feed_seconds: float = 10.0,
                    clock: RealClock | None = None) -> list[Row]:
    ep = endpoints or cfg.endpoints.build()
    clock = clock or RealClock()
    rows: list[Row] = []
    add = lambda n, s, d: rows.append(Row(n, s, d))  # noqa: E731

    add("config", "OK", f"mode={cfg.mode.mode}, auth={cfg.dhan.auth}, {len(cfg.instruments)} instruments, "
                        f"strategy={cfg.strategy.name or '-'}")
    holidays = load_holidays(cfg.resolve_path(cfg.session.holidays_file))
    today = clock.today()
    add("calendar", "OK" if is_trading_day(today, holidays) else "WARN",
        f"{today} is {'a trading day' if is_trading_day(today, holidays) else 'a weekend/holiday'}; "
        f"{len(holidays)} holidays loaded")

    if cfg.strategy.name:
        try:
            s = load_strategy(cfg.strategy.name, cfg.strategy.params, cfg.base_dir)
            syms, series = s.subscriptions()
            aliases = {i.alias for i in cfg.instruments}
            missing = [x for x in syms + [a for a, _ in series] if x not in aliases]
            add("strategy", "FAIL" if missing else "OK",
                f"{s.name}: needs symbols {missing} that are not in [[instruments]]" if missing else
                f"{s.name} loads; streams {syms}, bars {series}")
        except StrategyLoadError as e:
            add("strategy", "FAIL", str(e))

    tm = TokenManager(cfg, ep, clock)
    try:
        await tm.ensure(allow_login=cfg.dhan.auth == "totp")
        add("token", "OK" if tm.seconds_left() > 3600 else "WARN",
            f"from {tm.source}, valid for {tm.seconds_left() / 3600:.1f} h")
    except AuthError as e:
        add("token", "FAIL", str(e))
        return rows

    rest = DhanRest(ep, cfg.secrets.client_id, tm.token)
    await rest.start()
    try:
        t0 = time.perf_counter()
        prof = await rest.profile()
        rtt = (time.perf_counter() - t0) * 1000
        if prof.ok and isinstance(prof.data, dict):
            d = prof.data
            plan_ok = str(d.get("dataPlan", "")).lower() == "active"
            add("profile", "OK", f"client {d.get('dhanClientId')}, segments: {d.get('activeSegment')}, "
                                 f"first call {rtt:.0f} ms")
            add("data_plan", "OK" if plan_ok else "FAIL",
                f"dataPlan={d.get('dataPlan')} valid till {d.get('dataValidity')} (live market feed requires it)")
        else:
            add("profile", "FAIL", str(prof.error))

        ipres = await rest.get_ip()
        registered = set()
        if ipres.ok and isinstance(ipres.data, dict):
            registered = {str(ipres.data.get(k) or "") for k in ("primaryIP", "secondaryIP", "primaryIp",
                                                                   "secondaryIp")} - {""}
        mine = cfg.dhan.expected_ip
        if not mine and ep.is_live and cfg.dhan.public_ip_url:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(cfg.dhan.public_ip_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        mine = (await r.text()).strip()
            except Exception:
                mine = ""
        if not registered:
            add("static_ip", "FAIL" if cfg.is_live else "WARN",
                f"no static IP registered on Dhan - order APIs will be rejected. This host: {mine or '?'}. "
                f"Fix: dhanbot ip set {mine or '<ip>'}")
        elif mine and mine not in registered:
            add("static_ip", "FAIL" if cfg.is_live else "WARN",
                f"this host is {mine} but Dhan has {sorted(registered)} registered - orders from here are rejected")
        else:
            add("static_ip", "OK", f"registered {sorted(registered)}; this host {mine or 'unknown'}")

        funds = await rest.get_funds()
        if funds.ok and isinstance(funds.data, dict):
            add("funds", "OK", f"available ₹{available_balance(funds.data) or 0:,.2f}")
        else:
            add("funds", "WARN", str(funds.error))

        pnl = await rest.get_pnl_exit()
        add("pnl_exit", "INFO", f"current Dhan P&L-exit setting: {pnl.data if pnl.ok else pnl.error}")

        # instruments ----------------------------------------------------------------------
        segments = {i.segment for i in cfg.instruments} | set(cfg.instrument_master.extra_segments) | {"IDX_I"}
        try:
            store = await InstrumentStore.load(cfg, ep.instruments_csv_url, segments, today)
        except InstrumentError as e:
            add("instruments", "FAIL", str(e))
            return rows
        resolved = []
        for icfg in cfg.instruments:
            try:
                inst = store.resolve(icfg)
                resolved.append((icfg, inst))
                add(f"  {icfg.alias}", "OK",
                    f"{inst.segment} id={inst.security_id} {inst.trading_symbol!r} lot={inst.lot_size} "
                    f"tick=₹{inst.tick_paise / 100:.2f}" + (f" expiry={inst.expiry}" if inst.expiry else "")
                    + ("" if icfg.tradable and inst.tradable else " (signals only)"))
            except InstrumentError as e:
                add(f"  {icfg.alias}", "FAIL", str(e))
        tradable = [i for c, i in resolved if i.tradable and c.tradable]
        if tradable:
            body: dict[str, list[int]] = {}
            for i in tradable:
                body.setdefault(i.segment, []).append(i.security_id)
            q = await rest.quote(body)
            if q.ok and isinstance(q.data, dict):
                for i in tradable:
                    d = ((q.data.get("data") or {}).get(i.segment) or {}).get(str(i.security_id)) or {}
                    add(f"  {i.trading_symbol} quote", "OK" if d else "WARN",
                        f"ltp={d.get('last_price')} circuit {d.get('lower_circuit_limit')}..{d.get('upper_circuit_limit')}"
                        if d else "no quote returned")
            else:
                add("quotes", "WARN", str(q.error))

        # market feed + LTT detection ---------------------------------------------------------
        if resolved and feed_seconds > 0:
            frames = {"n": 0, "ticks": 0}
            norm = LttNormalizer("auto", clock)
            events: list[str] = []

            def on_frame(data: bytes) -> None:
                frames["n"] += 1
                for p in P.parse_frame(data):
                    if isinstance(p, P.Tick):
                        frames["ticks"] += 1
                        norm.observe(p.ltt)

            feed = FeedConnection("check", lambda: ep.feed_ws_url(cfg.secrets.client_id, tm.token), on_frame,
                                  on_event=lambda k, i: events.append(f"{k}:{i.get('reason', '')}"))
            for icfg, inst in resolved:
                feed.subscribe([(inst.segment, inst.security_id)], MODE_CODES[icfg.mode])
            task = asyncio.create_task(feed.run())
            await asyncio.sleep(feed_seconds)
            await feed.stop()
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
            fatal = [e for e in events if e.startswith("fatal")]
            add("market_feed", "FAIL" if fatal or not feed.frames else "OK",
                f"{frames['n']} frames / {frames['ticks']} ticks in {feed_seconds:.0f}s"
                + (f"; {fatal}" if fatal else "") + ("" if frames["ticks"] else " (market closed?)"))
            if frames["ticks"]:
                add("ltt_mode", "OK" if norm.detected else "WARN",
                    ("IST-shifted epoch" if norm.offset else "true UTC epoch") if norm.detected else
                    "could not detect (market closed / stale ticks) - leave feed.ltt_mode = auto")

        # order-update socket --------------------------------------------------------------------
        ou = OrderUpdateConnection(ep.order_ws_url, cfg.secrets.client_id, lambda: tm.token, lambda u: None)
        t = asyncio.create_task(ou.run())
        ok = False
        for _ in range(30):
            await asyncio.sleep(0.1)
            if ou.connected:
                await asyncio.sleep(1.0)  # a rejected login usually closes the socket immediately
                ok = ou.connected
                break
        await ou.stop()
        try:
            await asyncio.wait_for(t, 5)
        except (asyncio.TimeoutError, Exception):
            t.cancel()
        add("order_updates", "OK" if ok else "WARN", "login accepted" if ok else "could not stay connected")

        add("super_orders", "INFO", "support per segment is confirmed on the first super order; if Dhan rejects "
                                    "it, set execution.exit_mode = \"bot_managed\"")
    finally:
        await rest.close()
    return rows


def format_rows(rows: list[Row]) -> str:
    icon = {"OK": "✔", "WARN": "!", "FAIL": "✘", "INFO": "·"}
    w = max((len(r.name) for r in rows), default=10)
    return "\n".join(f" {icon.get(r.status, '?')} {r.status:<4} {r.name:<{w}}  {r.detail}" for r in rows)
