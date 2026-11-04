"""The engine: wires every component together and owns the trading session.

Modes
* ``live``   - real orders via Dhan REST, fills via the order-update WebSocket, reconciliation.
* ``paper``  - real Dhan market feed, simulated orders (PaperBroker). Needs a Dhan token for data.
* ``replay`` - recorded / synthetic frames pushed through ``on_frame`` with a VirtualClock (no network).

Hot path (all synchronous, no awaits, no queues)::

    feed WS frame -> parse_frame -> MarketState -> marks/P&L -> paper fills -> brackets -> bars
                  -> strategy.on_tick / on_bar -> ctx.buy() -> RiskEngine.check -> OMS.submit
                  -> loop.create_task(broker.place)  (the HTTP write happens on the next loop turn)
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import logging
import signal
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import orjson

from . import __version__
from .bars import BarAggregator, parse_timeframe
from .broker.live import LiveBroker
from .broker.paper import PaperBroker
from .clock import IST_OFFSET_S, LttNormalizer, RealClock, ist_datetime, load_holidays
from .config import AppConfig, InstrumentCfg
from .control import ControlServer
from .dhan.auth import TokenManager, write_private
from .dhan.endpoints import MODE_CODES, Endpoints
from .dhan.feed import FeedConnection
from .dhan.order_ws import OrderUpdateConnection
from .dhan import packets as P
from .dhan.packets import OIUpdate, PrevClose, Tick, parse_frame
from .dhan.rest import DhanRest
from .errors import Category, PreflightError
from .execution import marketable_price, sl_limit_price, user_limit_price
from .instruments import InstrumentStore
from .journal import Journal
from .latency import LatencyTracker, format_stats
from .market_state import MarketState
from .models import (LEG_STOP, LEG_TARGET, Bar, Instrument, Intent, Order, OrderEvent, OrderKind, OrderStatus,
                     OrderType, Product, Side, Validity)
from .notify.telegram import Telegram
from .oms import OrderManager
from .positions import ClosedTrade, PositionBook
from .ratelimit import AsyncLimiter, OrderRateLimiter
from .recorder import TickRecorder
from .reconcile import Reconciler
from .risk import KillSwitch, RiskEngine, TradingState
from .scheduler import Scheduler, SessionTimes
from .strategy.base import Strategy
from .strategy.context import Context
from .strategy.loader import load_strategy

log = logging.getLogger(__name__)

_EXIT_ROLES = ("exit", "squareoff", "flatten", "target", "sl")


@dataclass
class Bracket:
    """Bot-managed protective orders (execution.exit_mode = 'bot_managed')."""

    entry: Order
    sl_paise: int
    target_paise: int
    trail_paise: int
    sl_order: Order | None = None
    best_paise: float = 0.0
    active: bool = True
    stop_crossed_at: float = 0.0  # when price first crossed the stop trigger (for the gap chase)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True


@dataclass
class _Alert:
    ts: float
    level: str
    text: str


class Engine:
    def __init__(self, cfg: AppConfig, *, mode: str | None = None, clock: RealClock | None = None,
                 endpoints: Endpoints | None = None, instruments: InstrumentStore | None = None,
                 strategy: Strategy | None = None, use_journal: bool = True, use_control: bool = True,
                 use_telegram: bool = True, record: bool | None = None) -> None:
        self.cfg = cfg
        self.mode = mode or cfg.mode.mode
        if self.mode not in ("live", "paper", "replay"):
            raise ValueError("mode must be live | paper | replay")
        self.clock = clock or RealClock()
        self.ep = endpoints or cfg.endpoints.build()
        self.holidays = load_holidays(cfg.resolve_path(cfg.session.holidays_file))
        self.session = SessionTimes(cfg, self.holidays)
        self.scheduler = Scheduler(self.clock)
        self.market = MarketState()
        self.positions = PositionBook(cfg.charges)
        self.ks = KillSwitch(self.clock)
        self.latency = LatencyTracker()
        self.ltt = LttNormalizer(cfg.feed.ltt_mode, self.clock)
        self.bars = BarAggregator(self.session.anchor)
        self.instruments = instruments
        self.strategy = strategy
        self.use_journal, self.use_control, self.use_telegram = use_journal, use_control, use_telegram
        self.record = cfg.feed.record_ticks if record is None else record
        rate_now = time.monotonic if type(self.clock) is RealClock else self.clock.time
        self.limiter = OrderRateLimiter(cfg.rate_limits.orders_per_sec, cfg.rate_limits.orders_per_min,
                                        cfg.rate_limits.order_api_calls_per_day, rate_now)
        self.tokens: TokenManager | None = None
        self.rest: DhanRest | None = None
        self.broker: LiveBroker | PaperBroker | None = None
        self._paper: PaperBroker | None = None
        self.oms: OrderManager = None  # type: ignore[assignment]  (built in start)
        self.risk: RiskEngine = None  # type: ignore[assignment]
        self.reconciler: Reconciler | None = None
        self.feed: FeedConnection | None = None
        self.order_ws: OrderUpdateConnection | None = None
        self.journal: Journal | None = None
        self.recorder: TickRecorder | None = None
        self.telegram: Telegram | None = None
        self.control: ControlServer | None = None
        self.ctx: Context | None = None
        self.aliases: dict[str, Instrument] = {}
        self.limits: dict[tuple[int, int], InstrumentCfg] = {}
        self._alias_of: dict[tuple[int, int], str] = {}
        self._feed_modes: dict[tuple[int, int], int] = {}
        self._strategy_keys: set[tuple[int, int]] = set()
        self._bar_keys: set[tuple[int, int]] = set()
        self._strategy_series: set[tuple[tuple[int, int], int]] = set()
        self._strat_tick = False
        self.strategy_enabled = False
        self.strategy_errors = 0
        self._brackets: dict[int, Bracket] = {}
        self._bracket_by_key: dict[tuple[int, int], list[Bracket]] = {}
        self._exit_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._bg: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None
        self.stop_reason = ""
        self._frame_t0 = 0
        self.started_at = 0.0
        self.squareoff_done = False
        self._shutting_down = False
        self.alerts: deque[_Alert] = deque(maxlen=50)
        self.preflight_checks: list[Check] = []
        self._state_path: Path | None = None
        self._last_state_dump = ""

    # =============================================================================================
    # startup
    # =============================================================================================
    async def start(self) -> None:
        cfg = self.cfg
        self._stop_event = asyncio.Event()
        self.started_at = self.clock.time()
        data_dir = cfg.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        today = self.clock.today()

        if self.mode != "replay":
            self.tokens = TokenManager(cfg, self.ep, self.clock)
            session_end = self.session.today(self.clock.time())["session_end"]
            token = await self.tokens.ensure(valid_until=session_end + 1800)
            self.rest = DhanRest(self.ep, cfg.secrets.client_id or "", token,
                                 order_timeout_s=cfg.execution.order_timeout_ms / 1000.0,
                                 limiters={"non_trading": AsyncLimiter(cfg.rate_limits.non_trading_per_sec),
                                           "data": AsyncLimiter(cfg.rate_limits.data_per_sec),
                                           "quote": AsyncLimiter(cfg.rate_limits.quote_per_sec)})
            await self.rest.start()
            self.tokens.on_change(self._on_token_change)

        if self.use_journal:
            self.journal = Journal(data_dir / "journal.db")
            await self.journal.open(self.mode, __version__, self._config_hash())

        # instruments ------------------------------------------------------------------------------
        if self.instruments is None:
            segments = {i.segment for i in cfg.instruments} | set(cfg.instrument_master.extra_segments) | {"IDX_I"}
            self.instruments = await InstrumentStore.load(cfg, self.ep.instruments_csv_url, segments, today)
        for icfg in cfg.instruments:
            inst = self.instruments.resolve(icfg)
            self._register(icfg.alias, inst, icfg)

        # strategy ---------------------------------------------------------------------------------
        if self.strategy is None and cfg.strategy.name and cfg.strategy.enabled:
            self.strategy = load_strategy(cfg.strategy.name, cfg.strategy.params, cfg.base_dir)
        if self.strategy is not None:
            syms, series = self.strategy.subscriptions()
            for sym in syms:
                self._strategy_keys.add(self.resolve(sym).key)
            for sym, tf in series:
                inst = self.resolve(sym)
                s = self.bars.add(inst.key, self._alias_of.get(inst.key, sym), tf)
                self._bar_keys.add(inst.key)
                self._strategy_series.add((inst.key, s.tf))
            self._strat_tick = type(self.strategy).on_tick is not Strategy.on_tick

        # brokers / OMS / risk ------------------------------------------------------------------------
        if self.mode == "live":
            assert self.rest is not None
            self.broker = LiveBroker(self.rest, cfg.execution)
        else:
            self._paper = self.broker = PaperBroker(self.market, self.clock, cfg.paper)
        seq_start = 0
        if self.journal is not None:
            seq_start = await self.journal.max_cid_seq(f"{cfg.dhan.cid_prefix}{self.clock.now():%y%m%d}")
        self.oms = OrderManager(self.broker, self.limiter, self.positions, self.clock, cfg, seq_start=seq_start,
                                resolve_instrument=self._resolve_seg, latency=self.latency)
        self.oms.reprice_fn = self.reprice
        self.risk = RiskEngine(cfg, self.positions, self.market, self.clock, self.ks, limits=self.limits,
                               in_entry_window=self.session.in_entry_window,
                               token_seconds_left=(self.tokens.seconds_left if self.tokens else None),
                               working_exit_qty=self._working_exit_qty,
                               working_entries=self._working_entries)
        self.oms.listeners.append(self._on_order_event)
        self.oms.trade_listeners.append(self._on_trade_closed)
        self.oms.reject_listeners.append(self.risk.on_broker_reject)
        self.ks.listeners.append(self._on_killswitch)
        if self._paper is not None:
            self.risk.funds_available_inr = cfg.paper.starting_balance_inr

        # preflight (network checks) ---------------------------------------------------------------------
        if self.mode != "replay":
            self.preflight_checks = await self.preflight()
            failed = [c for c in self.preflight_checks if not c.ok and c.fatal]
            for c in self.preflight_checks:
                log.log(logging.INFO if c.ok else (logging.ERROR if c.fatal else logging.WARNING),
                        "preflight %-14s %s %s", c.name, "OK  " if c.ok else "FAIL", c.detail)
            if failed and self.mode == "live":
                raise PreflightError("preflight failed: " + "; ".join(f"{c.name}: {c.detail}" for c in failed))
            await self._warm_history()
            if self.mode == "live" and isinstance(self.broker, LiveBroker):
                for inst in self.aliases.values():
                    if inst.tradable and inst.key in self.limits:
                        self.broker.prewarm(Order(0, inst, Side.BUY, 1, product=Product(self.limits[inst.key].product)))
            assert self.rest is not None
            await self.rest.warm(2)

        # strategy start ---------------------------------------------------------------------------------
        self.ctx = Context(self)
        self._load_state()
        if self.strategy is not None:
            self.strategy_enabled = True
            self._call_strategy("on_start", self.ctx)
            open_pos = self.positions.open_positions()
            if open_pos:
                self._call_strategy("on_recover", self.ctx, open_pos)

        self._warm_hot_path()

        # background services ------------------------------------------------------------------------------
        await self._start_services()
        self._schedule_session()
        if cfg.perf.gc_freeze:
            gc.collect()
            gc.freeze()
            gc.set_threshold(cfg.perf.gc_gen0_threshold, 50, 100)
        self.alert(f"dhanbot {__version__} started in {self.mode.upper()} mode"
                   + (f" with strategy {self.strategy.name}" if self.strategy else " (no strategy)"), level="INFO")

    def _register(self, alias: str, inst: Instrument, icfg: InstrumentCfg | None) -> None:
        self.aliases[alias] = inst
        self._alias_of.setdefault(inst.key, alias)
        self.market.register(inst)
        if icfg is not None:
            if icfg.tradable and inst.tradable:
                self.limits[inst.key] = icfg
            mode = MODE_CODES[icfg.mode]
            if self.mode == "paper" and icfg.tradable and inst.tradable:
                mode = MODE_CODES["full"]  # depth gives realistic paper fills
            self._feed_modes[inst.key] = max(mode, self._feed_modes.get(inst.key, 0))

    async def preflight(self) -> list[Check]:
        """Account / connectivity checks. Fatal failures abort live trading."""
        out: list[Check] = []
        rest = self.rest
        assert rest is not None and self.tokens is not None
        hours = self.tokens.seconds_left() / 3600
        out.append(Check("token", hours > 0, f"source={self.tokens.source}, valid {hours:.1f} h"))
        prof = await rest.profile()
        if prof.ok and isinstance(prof.data, dict):
            plan = str(prof.data.get("dataPlan", "?"))
            out.append(Check("data_plan", plan.lower() == "active",
                             f"dataPlan={plan} validity={prof.data.get('dataValidity')} (market feed needs it)"))
            out.append(Check("segments", True, str(prof.data.get("activeSegment", "?")), fatal=False))
        else:
            out.append(Check("profile", False, str(prof.error)))
        if self.mode == "live":
            out.append(await self._check_static_ip())
        funds = await rest.get_funds()
        if funds.ok and isinstance(funds.data, dict):
            from .dhan.normalize import available_balance

            bal = available_balance(funds.data)
            if self.mode == "live":
                self.risk.funds_available_inr = bal
            out.append(Check("funds", bal is not None, f"available ₹{bal:,.2f}" if bal is not None else "unknown",
                             fatal=False))
        else:
            out.append(Check("funds", False, str(funds.error), fatal=False))
        out.append(await self._load_circuits())
        if self.mode == "live":
            assert isinstance(self.broker, LiveBroker)
            self.reconciler = Reconciler(self.cfg, rest, self.broker, self.oms, self.positions, self.risk,
                                         self._resolve_seg, self._bot_position_keys,
                                         lambda: bool(self.order_ws and self.order_ws.connected), self.alert)
            try:
                rep = await self.reconciler.startup()
                out.append(Check("recovery", True, f"adopted {rep['adopted_orders']} orders, "
                                                   f"{len(rep['positions'])} open bot positions, "
                                                   f"{rep['manual_positions']} manual positions untouched"))
            except Exception as e:
                out.append(Check("recovery", False, str(e)))
            if self.cfg.risk.pnl_exit_backstop:
                r = self.cfg.risk
                f = r.pnl_exit_backstop_factor
                profit = r.daily_profit_lock_inr * f if r.daily_profit_lock_inr > 0 else r.max_daily_loss_inr * 50
                res = await rest.set_pnl_exit(round(profit, 2), round(r.max_daily_loss_inr * f, 2), ["INTRADAY"], True)
                out.append(Check("pnl_backstop", res.ok, f"Dhan auto-exit at loss ₹{r.max_daily_loss_inr * f:,.0f}"
                                 if res.ok else f"could not register server-side P&L exit: {res.error}", fatal=False))
        return out

    async def _check_static_ip(self) -> Check:
        assert self.rest is not None
        res = await self.rest.get_ip()
        if not res.ok or not isinstance(res.data, dict):
            return Check("static_ip", False, f"could not read registered IPs: {res.error}")
        registered = {str(res.data.get(k) or "") for k in ("primaryIP", "secondaryIP", "primaryIp", "secondaryIp")} - {""}
        if not registered:
            return Check("static_ip", False, "no static IP registered on the account - orders will be rejected. "
                                             "Run `dhanbot ip set <this server's IP>`")
        mine = self.cfg.dhan.expected_ip
        if not mine and self.ep.is_live and self.cfg.dhan.public_ip_url:
            try:
                assert self.rest._session is not None
                import aiohttp

                async with self.rest.session.get(self.cfg.dhan.public_ip_url,
                                                 timeout=aiohttp.ClientTimeout(total=5)) as r:
                    mine = (await r.text()).strip()
            except Exception:
                mine = ""
        if not mine:
            return Check("static_ip", True, f"registered {sorted(registered)} (this host's IP unknown)", fatal=False)
        return Check("static_ip", mine in registered,
                     f"this host {mine}, registered {sorted(registered)}" +
                     ("" if mine in registered else " - orders from here WILL be rejected"))

    async def _load_circuits(self) -> Check:
        assert self.rest is not None
        tradable = [i for i in self.aliases.values() if i.tradable]
        if not tradable:
            return Check("circuits", True, "no tradable instruments", fatal=False)
        body: dict[str, list[int]] = {}
        for i in tradable:
            body.setdefault(i.segment, []).append(i.security_id)
        res = await self.rest.quote(body)
        if not res.ok or not isinstance(res.data, dict):
            return Check("circuits", False, f"quote API failed: {res.error}", fatal=False)
        data = res.data.get("data") or {}
        n = 0
        for i in tradable:
            q = (data.get(i.segment) or {}).get(str(i.security_id))
            st = self.market.of(i)
            if q and st is not None:
                st.upper_circuit = float(q.get("upper_circuit_limit") or 0)
                st.lower_circuit = float(q.get("lower_circuit_limit") or 0)
                if not st.ltp and q.get("last_price"):
                    st.ltp = float(q["last_price"])
                n += 1
        return Check("circuits", n == len(tradable), f"loaded for {n}/{len(tradable)} instruments", fatal=False)

    async def _warm_history(self) -> None:
        for key, tf in sorted(self._strategy_series):
            inst = self.market.get(key).inst  # type: ignore[union-attr]
            if tf % 60 or tf // 60 not in (1, 5, 15, 25, 60):
                log.info("history warm-up skipped for %s %ss (Dhan intraday API supports 1/5/15/25/60m)",
                         inst.trading_symbol, tf)
                continue
            bars = await self.fetch_history(inst, f"{tf // 60}m", 5)
            s = self.bars.get(key, tf)
            if s is not None and bars:
                s.load_history(bars)
                log.info("history: %d x %dm bars for %s", len(bars), tf // 60, inst.trading_symbol)

    async def fetch_history(self, inst: Instrument, timeframe: str = "1m", days: int = 5) -> list[Bar]:
        if self.rest is None:
            return []
        tf = parse_timeframe(timeframe)
        now = self.clock.now()
        frm = (now - timedelta(days=days)).strftime("%Y-%m-%d 09:00:00")
        res = await self.rest.intraday(inst.security_id, inst.segment, inst.instrument_type or "EQUITY", frm,
                                       now.strftime("%Y-%m-%d %H:%M:%S"), tf // 60)
        if not res.ok or not isinstance(res.data, dict):
            log.warning("history for %s failed: %s", inst.trading_symbol, res.error)
            return []
        d = res.data
        ts = d.get("timestamp") or []
        if not ts:
            return []
        # Dhan chart timestamps: detect IST-wall-clock-as-epoch (all candles would look like 14:45+ IST)
        first_minutes = min(ist_datetime(t).hour * 60 + ist_datetime(t).minute for t in ts)
        shift = IST_OFFSET_S if first_minutes >= 14 * 60 else 0
        alias = self._alias_of.get(inst.key, inst.trading_symbol)
        bars = [Bar(inst.key, alias, tf, float(t) - shift, float(o), float(h), float(lo), float(c), int(v or 0))
                for t, o, h, lo, c, v in zip(ts, d["open"], d["high"], d["low"], d["close"], d.get("volume") or [0] * len(ts), strict=False)]
        cutoff = self.clock.time()
        return [b for b in bars if b.start + tf <= cutoff]

    async def _start_services(self) -> None:
        cfg = self.cfg
        if self.mode != "replay":
            assert self.tokens is not None
            self.feed = FeedConnection("market", lambda: self.ep.feed_ws_url(cfg.secrets.client_id, self.tokens.token),
                                       self.on_frame, on_event=self._on_feed_event, heartbeat_s=cfg.feed.heartbeat_s,
                                       stale_after_s=cfg.feed.stale_after_s,
                                       expect_data=lambda: self.session.market_hours(self.clock.time()))
            for key, mode in self._feed_modes.items():
                inst = self.market.get(key).inst  # type: ignore[union-attr]
                self.feed.subscribe([(inst.segment, inst.security_id)], mode)
            self._bg.append(asyncio.create_task(self.feed.run(), name="feed"))
            if self.mode == "live":
                self.order_ws = OrderUpdateConnection(self.ep.order_ws_url, cfg.secrets.client_id,
                                                      lambda: self.tokens.token, self.oms.on_broker_update,
                                                      on_event=self._on_order_ws_event, on_raw=self._on_first_raw,
                                                      heartbeat_s=cfg.feed.heartbeat_s)
                self._bg.append(asyncio.create_task(self.order_ws.run(), name="order_ws"))
                if self.reconciler is not None:
                    self._bg.append(asyncio.create_task(self.reconciler.run(), name="reconcile"))
            self._bg.append(asyncio.create_task(self._keep_warm(), name="keep_warm"))
        if self.record and self.mode != "replay":
            self.recorder = TickRecorder(cfg.data_dir / "ticks" / f"{self.clock.today().isoformat()}.bin")
            self.recorder.start()
        if self.use_control:
            handlers = self.command_handlers()
            self.control = ControlServer(cfg.data_dir / "control.json", handlers)
            await self.control.start()
        if self.use_telegram and cfg.telegram.enabled:
            self.telegram = Telegram(cfg.secrets.telegram_token, cfg.secrets.telegram_chat_id, self.command_handlers(),
                                     cfg.telegram.max_messages_per_min, cfg.telegram.commands)
            await self.telegram.start()
        self._bg.append(asyncio.create_task(self._ticker(), name="ticker"))

    def _schedule_session(self) -> None:
        now = self.clock.time()
        t = self.session.today(now)
        if self.mode != "replay" and now >= t["session_end"]:
            # e.g. systemd restarted us after the close: never sit in a dead session
            self.request_stop("session already over")
            return
        if self.cfg.session.squareoff_enabled:
            # run_if_past: a restart after square-off time must still flatten inherited intraday positions
            self.scheduler.at(t["squareoff_time"], "square-off", self._squareoff, run_if_past=self.mode != "replay")
        self.scheduler.at(t["entry_end"], "entry window closed", lambda: self.alert("entry window closed", "INFO"))
        if self.mode != "replay":
            self.scheduler.at(t["session_end"], "session end", lambda: self.request_stop("session end"))

    # =============================================================================================
    # hot path
    # =============================================================================================
    def on_frame(self, data: bytes) -> None:
        t0 = time.perf_counter_ns()
        self._frame_t0 = t0
        rec = self.recorder
        if rec is not None:
            rec.add(data)
        now = self.clock.time()
        packets = parse_frame(data, t0)
        self.latency.record("frame_parse", time.perf_counter_ns() - t0)
        for p in packets:
            cls = p.__class__
            if cls is Tick:
                self._on_tick(p, now)
            elif cls is PrevClose:
                self.market.on_prev_close(p)
            elif cls is OIUpdate:
                st = self.market.get((p.seg, p.sid))
                if st is not None:
                    st.oi = p.oi
        self._frame_t0 = 0

    def _on_tick(self, t: Tick, now: float) -> None:
        ltt_norm = self.ltt
        if not ltt_norm.detected:
            ltt_norm.observe(t.ltt)
        ltt = ltt_norm.to_epoch(t.ltt)
        st = self.market.on_tick(t, now, ltt)
        if st is None:
            return
        key = (t.seg, t.sid)
        if self.positions.mark(key, int(t.ltp * 100 + 0.5)):
            self.risk.check_pnl()
        paper = self._paper
        if paper is not None and paper.has_working(key):
            paper.on_tick(st)
        brackets = self._bracket_by_key.get(key)
        if brackets:
            self._check_brackets(st, brackets)
        if key in self._bar_keys:
            bt = ltt if (ltt and abs(ltt - now) < 600) else now
            for bar in self.bars.on_tick(key, bt, t.ltp, t.volume if t.code != 2 else None):
                self._dispatch_bar(bar)
        if self._strat_tick and self.strategy_enabled and key in self._strategy_keys:
            s0 = time.perf_counter_ns()
            try:
                self.strategy.on_tick(self.ctx, t)  # type: ignore[union-attr]
            except Exception:
                self._strategy_failed("on_tick")
            dt = time.perf_counter_ns() - s0
            self.latency.record("strategy_on_tick", dt)
            if dt > self.cfg.perf.strategy_tick_budget_us * 1000 * 20:
                log.warning("strategy on_tick took %.0f us (budget %.0f us)", dt / 1000, self.cfg.perf.strategy_tick_budget_us)

    def _warm_hot_path(self, rounds: int = 300) -> None:
        """Run the order-building code a few hundred times with throw-away objects so CPython's
        adaptive interpreter has specialised it before the first real order (first calls are slower)."""
        t0 = time.perf_counter()
        frame_full = P.build_full(1, 1, 100.0, 0, bids=[(99.95, 1, 1)] * 5, asks=[(100.05, 1, 1)] * 5)
        frame_ticker = P.build_ticker(0, 1, 100.0, 0)
        for inst in [i for i in self.aliases.values() if i.tradable][:5]:
            st = self.market.of(inst)
            for _ in range(rounds):
                parse_frame(frame_full)
                parse_frame(frame_ticker)
                for side in (Side.BUY, Side.SELL):
                    px = marketable_price(inst, st, side, self.cfg.execution.entry_buffer_ticks) or inst.tick_paise
                    o = Order(0, inst, side, inst.lot_size, price_paise=px,
                              product=Product(self.limits[inst.key].product) if inst.key in self.limits else Product.INTRADAY)
                    self.risk.check(o, record=False)
                    if isinstance(self.broker, LiveBroker):
                        self.broker.build_body(o)
                    self.oms.next_cid()
        self.oms._seq -= rounds * 2 * len([i for i in self.aliases.values() if i.tradable][:5])  # give ids back
        log.debug("hot path warmed in %.1f ms", (time.perf_counter() - t0) * 1000)

    def _dispatch_bar(self, bar: Bar) -> None:
        if self.strategy_enabled and (bar.key, bar.tf) in self._strategy_series:
            self._call_strategy("on_bar", self.ctx, bar)

    # =============================================================================================
    # strategy plumbing
    # =============================================================================================
    def _call_strategy(self, method: str, *args: Any) -> None:
        if self.strategy is None or not self.strategy_enabled:
            return
        try:
            getattr(self.strategy, method)(*args)
        except Exception:
            self._strategy_failed(method)

    def _strategy_failed(self, where: str) -> None:
        self.strategy_errors += 1
        log.exception("strategy %s raised in %s", self.strategy.name if self.strategy else "?", where)
        if self.strategy_enabled:
            self.strategy_enabled = False
            self.alert(f"strategy disabled after an exception in {where} - exits, stops and risk keep running",
                       level="ERROR")

    def resolve(self, symbol: str | Instrument) -> Instrument:
        if isinstance(symbol, Instrument):
            return symbol
        inst = self.aliases.get(symbol) or self.aliases.get(symbol.upper())
        if inst is None:
            raise KeyError(f"unknown symbol {symbol!r}: add it to [[instruments]] in config "
                           f"(known: {sorted(self.aliases)})")
        return inst

    def _resolve_seg(self, segment: str | None, security_id: int | None) -> Instrument | None:
        if segment is None or security_id is None:
            return None
        from .dhan.endpoints import SEGMENT_TO_CODE

        code = SEGMENT_TO_CODE.get(segment)
        if code is None:
            return None
        st = self.market.get((code, int(security_id)))
        if st is not None:
            return st.inst
        return self.instruments.get(segment, int(security_id)) if self.instruments else None

    def add_instrument(self, inst: Instrument, mode: str = "quote", max_qty: int | None = None, tradable: bool = True,
                       bars: list[str] | None = None) -> str:
        alias = inst.trading_symbol
        if alias not in self.aliases:
            icfg = InstrumentCfg(alias=alias, segment=inst.segment, security_id=inst.security_id, mode=mode,
                                 product="INTRADAY", max_qty=max_qty if max_qty is not None else inst.lot_size,
                                 tradable=tradable and inst.tradable)
            self._register(alias, inst, icfg)
            if self.feed is not None:
                self.feed.subscribe([(inst.segment, inst.security_id)], self._feed_modes[inst.key])
        self._strategy_keys.add(inst.key)
        for tf in bars or []:
            s = self.bars.add(inst.key, alias, tf)
            self._bar_keys.add(inst.key)
            self._strategy_series.add((inst.key, s.tf))
        return alias

    def schedule_strategy_timer(self, hhmm: str, name: str) -> bool:
        from .clock import epoch_at

        when = epoch_at(self.clock.today(), hhmm)
        return self.scheduler.at(when, f"strategy timer {name}", lambda: self._call_strategy("on_timer", self.ctx, name))

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    # =============================================================================================
    # order placement
    # =============================================================================================
    def place(self, side: Side, symbol: str | Instrument, qty: int, price: float | None = None, *,
              order_type: str | None = None, product: str | None = None, trigger: float | None = None,
              sl: float | None = None, target: float | None = None, trail: float | None = None,
              validity: str | None = None, tag: str = "", role: str | None = None) -> Order:
        inst = self.resolve(symbol)
        cfg = self.cfg
        lim = self.limits.get(inst.key)
        prod = Product(product or (lim.product if lim else "INTRADAY"))
        pos_qty = self.positions.qty(inst, prod)
        intent = Intent.EXIT if pos_qty and ((pos_qty > 0) != (side is Side.BUY)) else Intent.ENTRY
        ot = OrderType((order_type or cfg.execution.default_order_type).upper())
        st = self.market.of(inst)
        buffer = cfg.execution.entry_buffer_ticks if intent is Intent.ENTRY else cfg.execution.exit_buffer_ticks
        price_paise = trigger_paise = 0
        if ot is OrderType.LIMIT:
            price_paise = user_limit_price(inst, price, side) if price else marketable_price(inst, st, side, buffer)
        elif ot in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET):
            if not trigger:
                raise ValueError(f"{ot.value} orders need trigger=")
            trigger_paise = inst.round_to_tick(trigger * 100)
            if ot is OrderType.STOP_LOSS:
                price_paise = (user_limit_price(inst, price, side) if price else
                               sl_limit_price(inst, trigger_paise, side, cfg.execution.sl_limit_buffer_ticks))
        kind = OrderKind.REGULAR
        sl_p = tg_p = tr_p = 0
        bracket = False
        if intent is Intent.ENTRY and (sl or target):
            sl_p = inst.round_to_tick(sl * 100) if sl else 0
            tg_p = inst.round_to_tick(target * 100) if target else 0
            tr_p = inst.round_to_tick(trail * 100) if trail else 0
            mode = cfg.execution.exit_mode
            if mode == "super_order" and getattr(self.broker, "supports_super", False):
                kind = OrderKind.SUPER
            elif mode == "bot_managed":
                bracket = True
            else:
                log.warning("sl/target ignored: execution.exit_mode = 'none'")
                sl_p = tg_p = tr_p = 0
        o = self.oms.create(inst, side, qty, order_type=ot, product=prod,
                            validity=Validity(validity or cfg.execution.validity), intent=intent, kind=kind,
                            price_paise=price_paise, trigger_paise=trigger_paise, target_paise=tg_p, sl_paise=sl_p,
                            trail_paise=tr_p, strategy=self.strategy.name if self.strategy else "", tag=tag,
                            role=role or ("entry" if intent is Intent.ENTRY else "exit"))
        o.meta["t_frame"] = self._frame_t0
        if ot is OrderType.LIMIT and price_paise <= 0:
            self.oms.reject_local(o, "no market price yet for a marketable limit (no tick received)")
            return o
        if intent is Intent.ENTRY and self.strategy is not None and not self.strategy_enabled and o.role == "entry":
            self.oms.reject_local(o, "strategy disabled")
            return o
        d = self.risk.check(o)
        if not d.ok:
            self.oms.reject_local(o, d.reason)
            log.info("risk rejected %s: %s", o, d.reason)
            return o
        if bracket:
            b = Bracket(o, sl_p, tg_p, tr_p)
            self._brackets[o.local_id] = b
        self.oms.submit(o)
        if self._frame_t0:
            self.latency.record("tick_to_submit", time.perf_counter_ns() - self._frame_t0)
        return o

    def reprice(self, order: Order) -> int:
        st = self.market.of(order.instrument)
        buf = self.cfg.execution.entry_buffer_ticks if order.intent is Intent.ENTRY else self.cfg.execution.exit_buffer_ticks
        return marketable_price(order.instrument, st, order.side, buf)

    def _working_entries(self, inst: Instrument) -> int:
        return sum(1 for o in self.oms.working(inst) if o.intent is Intent.ENTRY and o.role == "entry")

    def _working_exit_qty(self, inst: Instrument) -> int:
        return sum(o.remaining for o in self.oms.working(inst) if o.intent is Intent.EXIT)

    # =============================================================================================
    # order events, brackets
    # =============================================================================================
    def _on_order_event(self, ev: OrderEvent) -> None:
        o = ev.order
        j = self.journal
        if j is not None:
            j.order(o)
            if ev.fill is not None:
                j.fill(ev.fill, o)
        if ev.kind == "fill":
            f = ev.fill
            assert f is not None
            self.risk.check_pnl()
            if self._paper is not None:
                self.risk.funds_available_inr = self.cfg.paper.starting_balance_inr + self.positions.net_paise() / 100
            self.alert(f"FILL {f.side.value} {f.qty} {o.instrument.trading_symbol} @ {f.price_paise / 100:.2f}"
                       f" ({o.role}{'/' + f.leg if f.leg not in ('MAIN',) else ''})", level="INFO", journal=False)
        elif ev.kind == "rejected" and not o.meta.get("local_reject"):
            self.alert(f"REJECTED {o.side.value} {o.qty} {o.instrument.trading_symbol}: {o.reason}", level="WARNING")
            if o.meta.get("error_category") == Category.AUTH.value:
                self.spawn(self.refresh_token("order rejected with an auth error"))
            elif o.meta.get("error_category") == Category.IP_NOT_ALLOWED.value:
                self.ks.halt("Dhan rejected an order: this host's IP is not the registered static IP")
        elif ev.kind in ("failed", "unknown"):
            self.alert(f"order {o.cid} {ev.kind}: {o.reason}", level="WARNING")
        b = self._brackets.get(o.local_id)
        if b is not None and o.is_done:
            self._arm_bracket(b)
        if o.role == "sl" and o.status is OrderStatus.FILLED and o.parent_id in self._brackets:
            self._deactivate_bracket(self._brackets[o.parent_id])
        if self.strategy_enabled:
            self._call_strategy("on_order", self.ctx, ev)

    def _on_trade_closed(self, trade: ClosedTrade, order: Order) -> None:
        self.risk.on_trade_closed(trade, order)
        if self.journal is not None:
            self.journal.event("trade_closed", {"symbol": trade.inst.trading_symbol, "pnl": round(trade.pnl_paise / 100, 2),
                                                "qty": trade.qty})

    def _arm_bracket(self, b: Bracket) -> None:
        if not b.active or b.sl_order is not None:
            return
        entry = b.entry
        if entry.filled_qty <= 0:
            self._deactivate_bracket(b)
            return
        b.best_paise = entry.avg_fill_paise
        self._bracket_by_key.setdefault(entry.instrument.key, []).append(b)
        if b.sl_paise:
            exit_side = entry.side.opposite
            price = sl_limit_price(entry.instrument, b.sl_paise, exit_side, self.cfg.execution.sl_limit_buffer_ticks)
            sl = self.oms.create(entry.instrument, exit_side, entry.filled_qty, order_type=OrderType.STOP_LOSS,
                                 product=entry.product, intent=Intent.EXIT, price_paise=price,
                                 trigger_paise=b.sl_paise, strategy=entry.strategy, role="sl",
                                 parent_id=entry.local_id, tag="bracket-sl")
            d = self.risk.check(sl)
            if not d.ok:
                self.oms.reject_local(sl, d.reason)
                self.alert(f"could not place bracket stop for {entry.cid}: {d.reason}", level="ERROR")
            else:
                self.oms.submit(sl)
            b.sl_order = sl

    def _deactivate_bracket(self, b: Bracket) -> None:
        b.active = False
        lst = self._bracket_by_key.get(b.entry.instrument.key)
        if lst and b in lst:
            lst.remove(b)

    def _check_brackets(self, st, brackets: list[Bracket]) -> None:
        px = st.ltp * 100
        for b in list(brackets):
            if not b.active:
                continue
            e = b.entry
            sign = e.side.sign
            if b.target_paise and (px - b.target_paise) * sign >= 0:
                self._deactivate_bracket(b)
                self.spawn(self.exit_position(e.instrument, e.product, role="target", reason="bracket target"))
                continue
            slo = b.sl_order
            if slo is not None and not slo.is_done and b.sl_paise and (b.sl_paise - px) * sign >= 0:
                # Price is through the stop. A stop-LIMIT can be skipped on a gap; if price is already
                # beyond its limit (or it has not filled within 3s), chase with a marketable exit.
                now = self.clock.time()
                if not b.stop_crossed_at:
                    b.stop_crossed_at = now
                beyond_limit = slo.price_paise and (slo.price_paise - px) * sign > 0
                if beyond_limit or now - b.stop_crossed_at > 3.0:
                    self._deactivate_bracket(b)
                    self.alert(f"stop for {e.instrument.trading_symbol} gapped (ltp {px / 100:.2f}, stop "
                               f"{b.sl_paise / 100:.2f}) - chasing with a marketable exit", level="WARNING")
                    self.spawn(self.exit_position(e.instrument, e.product, role="sl", reason="stop gap chase"))
                continue
            if slo is not None and slo.is_done:
                continue
            if b.trail_paise and b.sl_order is not None and not b.sl_order.is_done and b.sl_order.broker_id:
                if (px - b.best_paise) * sign > 0:
                    b.best_paise = px
                dist = abs(e.avg_fill_paise - b.sl_paise)
                new_sl = e.instrument.round_to_tick(b.best_paise - sign * dist)
                if (new_sl - b.sl_order.trigger_paise) * sign >= b.trail_paise:
                    b.sl_paise = new_sl
                    price = sl_limit_price(e.instrument, new_sl, e.side.opposite, self.cfg.execution.sl_limit_buffer_ticks)
                    self.oms.modify(b.sl_order, trigger_paise=new_sl, price_paise=price)

    # =============================================================================================
    # exits / square-off / halt
    # =============================================================================================
    async def exit_position(self, inst: Instrument, product: Product | None = None, role: str = "exit",
                            reason: str = "", reprice: bool = True) -> bool:
        """Flatten one instrument: cancel its working orders and protective legs (and wait for the
        cancels to confirm, so a stop filling meanwhile cannot make us over-exit), then send a
        marketable-limit exit that is re-priced until filled (market order as a last resort)."""
        lock = self._exit_locks.setdefault(inst.key, asyncio.Lock())
        async with lock:
            await self._cancel_instrument_orders(inst)
            for b in list(self._bracket_by_key.get(inst.key, [])):
                self._deactivate_bracket(b)
            products = [product] if product else sorted({p.product for p in self.positions.open_positions()
                                                         if p.inst.key == inst.key}, key=lambda p: p.value)
            done = True
            for prod in products:
                qty = self.positions.qty(inst, prod)
                if qty == 0:
                    continue
                if not await self._exit_qty(inst, prod, qty, role, reason, reprice):
                    done = False
            return done and self.positions.qty(inst) == 0

    async def _cancel_instrument_orders(self, inst: Instrument) -> None:
        futs: list[asyncio.Future] = []
        waits: list[Order] = []
        for o in self.oms.working(inst):
            futs.append(self.oms.cancel(o))
            waits.append(o)
        supers = [o for o in self.oms.orders.values() if o.instrument.key == inst.key and o.kind is OrderKind.SUPER
                  and o.is_done and self.oms.super_exit_legs_working(o)]
        for o in supers:
            futs.append(self.oms.cancel(o, LEG_TARGET))
            futs.append(self.oms.cancel(o, LEG_STOP))
        if not futs:
            return
        await asyncio.gather(*futs, return_exceptions=True)
        ok = await self.oms.wait_until(lambda: all(o.is_done for o in waits) and
                                       not any(self.oms.super_exit_legs_working(s) for s in supers), timeout=3.0)
        if not ok:
            log.warning("exit %s: some orders did not confirm cancellation within 3s", inst.trading_symbol)

    async def _exit_qty(self, inst: Instrument, prod: Product, qty: int, role: str, reason: str, reprice: bool) -> bool:
        side = Side.SELL if qty > 0 else Side.BUY
        st = self.market.of(inst)
        px = marketable_price(inst, st, side, self.cfg.execution.exit_buffer_ticks)
        ot = OrderType.LIMIT if px > 0 else OrderType.MARKET
        o = self.oms.create(inst, side, abs(qty), order_type=ot, product=prod, intent=Intent.EXIT, price_paise=px,
                            strategy=self.strategy.name if self.strategy else "", role=role, tag=reason[:40])
        d = self.risk.check(o)
        if not d.ok:
            self.oms.reject_local(o, d.reason)
            self.alert(f"exit of {inst.trading_symbol} blocked: {d.reason}", level="ERROR")
            return False
        self.oms.submit(o)
        if not reprice or ot is OrderType.MARKET:
            return await self.oms.wait_until(lambda: o.is_done, timeout=5.0) and o.status is OrderStatus.FILLED
        ex = self.cfg.execution
        for _ in range(ex.squareoff_max_reprices):
            if await self.oms.wait_until(lambda: o.is_done, timeout=ex.squareoff_reprice_s):
                break
            new = self.reprice(o)
            if new and new != o.price_paise and o.broker_id:
                await self.oms.modify(o, price_paise=new)
        if not o.is_done and ex.squareoff_market_fallback:
            await self.oms.cancel(o)
            await self.oms.wait_until(lambda: o.is_done, timeout=3.0)
            left = self.positions.qty(inst, prod)
            if left:
                m = self.oms.create(inst, Side.SELL if left > 0 else Side.BUY, abs(left), order_type=OrderType.MARKET,
                                    product=prod, intent=Intent.EXIT, role=role, tag="market fallback",
                                    strategy=o.strategy)
                if self.risk.check(m).ok:
                    self.oms.submit(m)
                    await self.oms.wait_until(lambda: m.is_done, timeout=5.0)
        return self.positions.qty(inst, prod) == 0

    async def flatten_all(self, role: str = "flatten", reason: str = "", reprice: bool = True,
                          products: set[Product] | None = None) -> bool:
        targets = {p.inst.key: p.inst for p in self.positions.open_positions()
                   if products is None or p.product in products}
        # also instruments that only have working orders / legs (exit_position cancels them)
        for o in self.oms.orders.values():
            if (not o.is_done and o.status is not OrderStatus.NEW) or self.oms.super_exit_legs_working(o):
                targets.setdefault(o.instrument.key, o.instrument)
        if not targets:
            return True
        self.alert(f"flattening {len(targets)} instrument(s): {reason}", level="WARNING")
        results = await asyncio.gather(*(self.exit_position(i, role=role, reason=reason, reprice=reprice)
                                         for i in targets.values()), return_exceptions=True)
        ok = all(r is True for r in results)
        left = [p.to_dict() for p in self.positions.open_positions() if products is None or p.product in products]
        if left:
            self.alert(f"flatten incomplete, still open: {left}", level="ERROR")
        return ok and not left

    def _squareoff(self) -> None:
        self.squareoff_done = True
        self.ks.pause("square-off time")
        self.spawn(self.flatten_all(role="squareoff", reason="scheduled square-off",
                                    products={Product.INTRADAY}))

    def _on_killswitch(self, state: TradingState, reason: str) -> None:
        self.alert(f"trading {state.value}: {reason}", level="ERROR" if state is TradingState.HALTED else "WARNING")
        if state is TradingState.HALTED:
            self.spawn(self._halt_actions(reason))

    async def _halt_actions(self, reason: str) -> None:
        await self.flatten_all(role="flatten", reason=f"HALT: {reason}")
        if self.mode == "live" and self.rest is not None:
            if self.cfg.execution.flatten_scope == "all" and isinstance(self.broker, LiveBroker):
                await self.broker.exit_all()
            if self.cfg.risk.broker_killswitch_on_halt:
                res = await self.rest.killswitch(True)
                self.alert("Dhan kill switch activated for the day" if res.ok else f"Dhan kill switch failed: {res.error}",
                           level="ERROR")

    # =============================================================================================
    # connectivity events
    # =============================================================================================
    def _on_feed_event(self, kind: str, info: dict) -> None:
        if kind == "connected":
            log.info("market feed connected (%d instruments)", info.get("instruments", 0))
        elif kind == "token_expired":
            self.spawn(self.refresh_token("feed reported token expiry"))
        elif kind == "fatal":
            self.alert(f"market feed stopped: {info.get('reason')} {info.get('text')}", level="ERROR")
            self.ks.pause(f"market feed unavailable ({info.get('text')})")
        elif kind in ("unstable", "stale"):
            self.alert(f"market feed {kind}: {info}", level="WARNING")

    def _on_order_ws_event(self, kind: str, info: dict) -> None:
        if self._shutting_down:
            return
        if kind == "disconnected":
            self.alert("order-update socket disconnected - polling the order book every second", level="WARNING")
        elif kind == "connected":
            log.info("order-update socket connected")

    def _on_first_raw(self, data: dict) -> None:
        if self.journal is not None:
            self.journal.event("order_update_sample", data)

    def _on_token_change(self, token: str) -> None:
        if self.rest is not None:
            self.rest.set_token(token)

    async def refresh_token(self, why: str) -> None:
        if self.tokens is None:
            return
        log.warning("refreshing access token: %s", why)
        try:
            await self.tokens.ensure(valid_until=self.clock.time() + 3600 * 6)
        except Exception as e:
            self.alert(f"token refresh failed ({e}) - new entries blocked until a valid token is available",
                       level="ERROR")
            return
        if self.feed is not None:
            await self.feed.recycle()
        if self.order_ws is not None:
            await self.order_ws.recycle()

    # =============================================================================================
    # periodic work
    # =============================================================================================
    async def _ticker(self) -> None:
        n = 0
        kill_file = self.cfg.data_dir / "KILL"
        last_lat = last_pnl = self.clock.time()
        while True:
            await self.clock.sleep(0.25)
            n += 1
            now = self.clock.time()
            for bar in self.bars.flush(now):
                self._dispatch_bar(bar)
            if n % 4 == 0 and self.mode != "replay" and kill_file.exists():
                self.ks.halt("KILL file present")
                try:
                    kill_file.rename(kill_file.with_suffix(".done"))
                except OSError:
                    pass
            if now - last_pnl >= 60:
                last_pnl = now
                b = self.positions
                if self.journal is not None:
                    self.journal.pnl(now, b.realised_paise() / 100, b.unrealised_paise() / 100, b.charges_paise() / 100,
                                     b.net_paise() / 100, len(b.open_positions()))
                self._save_state()
            if now - last_lat >= self.cfg.perf.latency_report_s:
                last_lat = now
                stats = self.latency.roll()
                if stats:
                    log.info("latency (last %.0fs):\n%s", self.cfg.perf.latency_report_s, format_stats(stats))
                    if self.journal is not None:
                        self.journal.latency(now, stats)

    async def _keep_warm(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.execution.keep_warm_s)
            if self.rest is not None:
                res = await self.rest.profile()
                if not res.ok and res.error and res.error.category is Category.AUTH:
                    await self.refresh_token("keep-alive call got an auth error")

    # =============================================================================================
    # control / status
    # =============================================================================================
    def status(self) -> dict:
        b = self.positions
        lat = {s.stage: f"p50 {s.p50_us:.0f}us p99 {s.p99_us:.0f}us" for s in self.latency.snapshot()}
        return {
            "mode": self.mode, "state": self.ks.state.value, "reason": self.ks.reason,
            "time": self.clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            "strategy": self.strategy.name if self.strategy else None, "strategy_enabled": self.strategy_enabled,
            "net_pnl": round(b.net_paise() / 100, 2), "realised": round(b.realised_paise() / 100, 2),
            "unrealised": round(b.unrealised_paise() / 100, 2), "charges": round(b.charges_paise() / 100, 2),
            "open_positions": len(b.open_positions()), "working_orders": len(self.oms.working()),
            "orders": self.oms.counts(), "order_slots_left_today": self.limiter.remaining_today,
            "feed": bool(self.feed and self.feed.connected), "order_ws": bool(self.order_ws and self.order_ws.connected),
            "token_hours_left": round(self.tokens.seconds_left() / 3600, 1) if self.tokens else None,
            "uptime_min": round((self.clock.time() - self.started_at) / 60, 1), "latency": lat,
        }

    def command_handlers(self) -> dict:
        async def flatten(req: dict) -> dict:
            self.ks.pause(f"manual flatten ({req.get('source', 'cli')})")
            ok = await self.flatten_all(role="flatten", reason="manual flatten")
            return {"flattened": ok, "note": "entries paused - send resume to allow new entries"}

        def kill(req: dict) -> dict:
            self.ks.halt(f"manual kill ({req.get('source', 'cli')})")
            return {"state": self.ks.state.value}

        def pause(req: dict) -> dict:
            self.ks.pause(f"manual pause ({req.get('source', 'cli')})")
            return {"state": self.ks.state.value}

        def resume(req: dict) -> dict:
            ok = self.ks.resume()
            return {"state": self.ks.state.value, "resumed": ok}

        return {
            "status": lambda req: self.status(),
            "pnl": lambda req: {k: v for k, v in self.status().items() if k in ("net_pnl", "realised", "unrealised", "charges")},
            "positions": lambda req: {"positions": [p.to_dict() for p in self.positions.open_positions()]},
            "orders": lambda req: {"orders": [repr(o) for o in self.oms.working()]},
            "pause": pause, "resume": resume, "flatten": flatten, "kill": kill,
            "stop": lambda req: (self.request_stop("stop command"), {"stopping": True})[1],
        }

    def alert(self, text: str, level: str = "WARNING", journal: bool = True) -> None:
        log.log(getattr(logging, level, logging.WARNING), "%s", text)
        self.alerts.append(_Alert(self.clock.time(), level, text))
        if journal and self.journal is not None:
            self.journal.event("alert", {"text": text}, level=level)
        if self.telegram is not None:
            icon = {"ERROR": "🛑", "WARNING": "⚠️"}.get(level, "ℹ️")
            self.telegram.send(f"{icon} [{self.mode}] {text}")

    # =============================================================================================
    # lifecycle
    # =============================================================================================
    def request_stop(self, reason: str) -> None:
        if not self.stop_reason:
            self.stop_reason = reason
            log.info("stop requested: %s", reason)
        if self._stop_event is not None:
            self._stop_event.set()

    async def run(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop, f"signal {sig.name}")
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        try:
            assert self._stop_event is not None
            await self._stop_event.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> dict:
        if self._shutting_down:
            return {}
        self._shutting_down = True
        log.info("shutting down (%s)", self.stop_reason or "requested")
        self.scheduler.cancel_all()
        if self.ks.state is TradingState.RUNNING:
            self.ks.pause("shutting down")
        if self.oms is not None:
            open_intraday = [p for p in self.positions.open_positions() if p.product is Product.INTRADAY]
            if self.mode != "replay" and self.cfg.execution.flatten_on_shutdown and open_intraday:
                try:
                    await asyncio.wait_for(self.flatten_all(role="flatten", reason="shutdown",
                                                            products={Product.INTRADAY}), timeout=30)
                except asyncio.TimeoutError:
                    self.alert("shutdown flatten timed out", level="ERROR")
            elif self.mode != "replay":
                for o in self.oms.working():
                    if o.intent is Intent.ENTRY:
                        self.oms.cancel(o)
                await asyncio.sleep(0.2)
        if self.strategy is not None and self.ctx is not None:
            was = self.strategy_enabled
            self.strategy_enabled = True
            self._call_strategy("on_stop", self.ctx)
            self.strategy_enabled = was
        self._save_state()
        report = self.daily_report()
        self.alert(self.report_text(report), level="INFO")
        rdir = self.cfg.data_dir / "reports"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{self.clock.today().isoformat()}-{self.mode}-{int(self.started_at)}.json").write_bytes(
            orjson.dumps(report, option=orjson.OPT_INDENT_2, default=str))
        for comp in (self.feed, self.order_ws):
            if comp is not None:
                await comp.stop()
        if self.reconciler is not None:
            self.reconciler.stop()
        for t in self._bg:
            t.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.oms is not None:
            await self.oms.close()
        if self.recorder is not None:
            await self.recorder.close()
        if self.journal is not None:
            await self.journal.close()
        if self.control is not None:
            await self.control.stop()
        if self.telegram is not None:
            await self.telegram.stop()
        if self.rest is not None:
            await self.rest.close()
        if self.cfg.perf.gc_freeze:
            gc.unfreeze()
        return report

    def daily_report(self) -> dict:
        b = self.positions
        return {
            "date": self.clock.today().isoformat(), "mode": self.mode, "stop_reason": self.stop_reason,
            "strategy": self.strategy.name if self.strategy else None, "strategy_errors": self.strategy_errors,
            "state": self.ks.state.value, "state_reason": self.ks.reason,
            "pnl": {"realised": round(b.realised_paise() / 100, 2), "unrealised": round(b.unrealised_paise() / 100, 2),
                    "charges_est": round(b.charges_paise() / 100, 2), "net": round(b.net_paise() / 100, 2)},
            "trades": {"closed": self.risk.closed_trades if self.risk else 0, "wins": self.risk.wins if self.risk else 0},
            "orders": self.oms.counts() if self.oms else {}, "fills": b.fills,
            "turnover": round(b.turnover_paise / 100, 2),
            "open_positions": [p.to_dict() for p in b.open_positions()],
            "risk_rejections": dict(self.risk.rejections) if self.risk else {},
            "latency": [s.as_dict() for s in self.latency.day()],
            "ltt_mode": "ist_shifted" if self.ltt.offset else "utc",
            "feed_frames": self.feed.frames if self.feed else None,
        }

    @staticmethod
    def report_text(r: dict) -> str:
        p = r["pnl"]
        return (f"Day report {r['date']} [{r['mode']}]: net ₹{p['net']:,.2f} (realised {p['realised']:,.2f}, "
                f"unrealised {p['unrealised']:,.2f}, est. charges {p['charges_est']:,.2f}); "
                f"trades {r['trades']['closed']} (wins {r['trades']['wins']}); fills {r['fills']}; "
                f"open positions {len(r['open_positions'])}; final state {r['state']}")

    # ---- strategy state persistence ----------------------------------------------------------------
    def _load_state(self) -> None:
        if self.strategy is None or self.ctx is None or self.mode == "replay":
            return
        self._state_path = self.cfg.data_dir / "state" / f"{self.strategy.name}.json"
        try:
            d = orjson.loads(self._state_path.read_bytes())
            if d.get("date") == self.clock.today().isoformat():
                self.ctx.state.update(d.get("state") or {})
                log.info("restored strategy state from %s", self._state_path)
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("ignoring unreadable strategy state %s", self._state_path)

    def _save_state(self) -> None:
        if self._state_path is None or self.ctx is None:
            return
        try:
            blob = orjson.dumps({"date": self.clock.today().isoformat(), "state": self.ctx.state}, default=str)
        except TypeError:
            log.warning("strategy state is not JSON-serialisable; not saved")
            return
        if blob.decode() != self._last_state_dump:
            write_private(self._state_path, blob)
            self._last_state_dump = blob.decode()

    # ---- helpers --------------------------------------------------------------------------------------
    def _bot_position_keys(self) -> set[tuple[int, int, str]]:
        return {(k[0], k[1], icfg.product) for k, icfg in self.limits.items()}

    def _config_hash(self) -> str:
        import dataclasses

        d = dataclasses.asdict(self.cfg)
        d.pop("secrets", None)
        return hashlib.sha1(orjson.dumps(d, default=str, option=orjson.OPT_SORT_KEYS)).hexdigest()[:12]
