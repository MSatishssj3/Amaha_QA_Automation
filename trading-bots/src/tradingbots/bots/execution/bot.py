"""Execution bot: runs your ``dhan_bot`` trading engine as a member of the team.

* The engine is exactly the one in ``dhan_bot``: strategy, risk rules, order manager, super orders
  and square-off. This bot hosts it and attaches the **BudgetGuard** before the strategy can place
  its first order. Order, fill, state, P&L and rejection events go to the bus from a queue, off the
  hot path.
* It is opt-in: set ``execution.enabled = true``.
* **Real orders** on Dhan need ``execution.live = true`` **and** ``mode = "live"`` in the dhan_bot
  config. Anything else runs in paper mode.
* On the **simulated** platform, orders go to the simulated exchange, so fills, trade book and
  charges behave like a broker's. No real money is involved.
* Credentials come from the config bot (``secret:token``). The engine never logs in or renews by
  itself, so the two can never invalidate each other's tokens.
* It runs one session per trading day, from the dhan_bot ``preflight`` time to ``session_end``.
* A platform or execution-settings change restarts the engine, but only when no positions or orders
  are open. Otherwise it waits for the next session.
* Controls: ``cmd.execution.control {action: status | pause | resume | flatten | kill}``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import tomllib
from collections import deque
from datetime import date
from typing import Any

from dhanbot.clock import epoch_at, ist_datetime
from dhanbot.config import AppConfig, load_config as load_dhan_config
from dhanbot.dhan.auth import TokenManager
from dhanbot.engine import Engine
from dhanbot.errors import ConfigError
from dhanbot.models import Order, OrderEvent
from dhanbot.risk import TradingState

from ...core import topics
from ...core.bot import Bot
from ...core.bus import Message
from ...core.settings import Settings
from ...platforms.registry import endpoints_from
from .guard import BudgetGuard

REJECT_THROTTLE_S = 60.0  # at most one exec.reject per (symbol, rule) per minute, with a count


class ExecutionBot(Bot):
    name = "execution"

    def configure(self) -> None:
        self.on(topics.BUDGET_POLICY, self.on_policy)
        self.on(topics.BUDGET_EXTERNAL, self.on_external)
        self.on(topics.AUTH_TOKEN, self.on_token)
        self.command("control", self.cmd_control)
        self.command("status", self.cmd_status)
        self.every(2.0, self.publish_state, name="state", wall_clock=True)
        self.engine: Engine | None = None
        self.guard: BudgetGuard | None = None
        self.exec_mode = ""  # paper | live | simulated-exchange
        self.platform_name = ""
        self.note = ""
        self._events: asyncio.Queue = asyncio.Queue(maxsize=100_000)
        self._token_version = 0
        self._endpoints_info: dict | None = None
        self._cid_prefix = "DB"
        self._loop_task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self._session_done: date | None = None
        self._restart = False
        self._rejects: dict[tuple[str, str], list] = {}
        self._fwd_alerts: deque[tuple[float, str]] = deque(maxlen=200)
        self._last_pnl: dict | None = None
        self._last_pnl_ts = 0.0
        self._placed: dict[str, float] = {}  # cid -> when the order was first seen (placed)
        self._legs_sent: dict[str, tuple] = {}  # cid -> super-order leg statuses last published

    def details(self) -> dict:
        e = self.engine
        return {"note": self.note, "mode": self.exec_mode or None, "platform": self.platform_name or None,
                "state": e.ks.state.value if e is not None else None,
                "guard": self.guard.usage() if self.guard else None}

    async def setup(self) -> None:
        self.guard = BudgetGuard(self.settings.platform.name, self.clock)
        policy = await self.bus.kv_get(topics.KV_BUDGET_POLICY)
        if policy:
            self.guard.update_policy(policy)
        ext = await self.bus.kv_get(topics.KV_BUDGET_EXTERNAL)
        if ext:
            self.guard.update_external(ext)
        self.guard.seed(await self.bus.kv_get(topics.KV_BUDGET_USAGE) or {})
        self.spawn(self._publisher(), name="events")
        if not self.settings.execution.enabled:
            self.note = "disabled (set execution.enabled = true to run your dhan_bot strategy here)"
        else:
            self._loop_task = self.spawn(self._lifecycle(), name="lifecycle")
        await self.publish_state()

    async def teardown(self) -> None:
        await self._stop_engine("execution bot stopping")
        await self.publish_state()

    async def on_settings(self, old: Settings, new: Settings) -> None:
        if not new.execution.enabled:
            if old.execution.enabled:
                if self._loop_task is not None:
                    self._loop_task.cancel()
                self.note = "disabled in settings"
                self.spawn(self._stop_engine("execution disabled in settings"), name="stop")
            return
        if not old.execution.enabled or self._loop_task is None or self._loop_task.done():
            self._session_done = None
            self._loop_task = self.spawn(self._lifecycle(), name="lifecycle")
            return
        if (old.execution != new.execution or old.platform.name != new.platform.name) and self.engine is not None:
            if self._busy(self.engine):
                await self.alert("execution / platform settings changed while positions or orders are open - "
                                 "the trading engine keeps its current setup until the next session", "WARNING",
                                 key="exec-deferred")
            else:
                self._restart = True
                self.spawn(self._stop_engine("settings changed - restarting the trading engine"), name="restart")

    @staticmethod
    def _busy(e: Engine) -> bool:
        return bool(e.oms is not None and (e.positions.open_positions() or e.oms.working()))

    # ---- engine lifecycle -----------------------------------------------------------------------------------
    async def _lifecycle(self) -> None:
        while not self._stop.is_set() and self.settings.execution.enabled:
            try:
                cfg, info, mode = await self._prepare()
            except Exception as e:
                self.note = f"cannot start: {e}"
                self._error("prepare engine", e)
                await self.alert(f"trading engine cannot start: {e}", "ERROR", key="exec-start")
                await self.clock.sleep(300.0)
                continue
            times = cfg.session_times()
            now, today = self.clock.time(), self.ctx.today()
            start_at, end_at = epoch_at(today, times["preflight"]), epoch_at(today, times["session_end"])
            if self._session_done == today or not self.ctx.calendar.is_trading_day(today) or now >= end_at:
                nxt = self.ctx.calendar.next_time(now, times["preflight"])
                self.note = f"waiting for the next session ({ist_datetime(nxt):%a %d %b %H:%M} IST)"
                await self.clock.sleep(max(1.0, nxt - now))
                continue
            if now < start_at:
                self.note = f"session starts at {times['preflight']} IST"
                await self.clock.sleep(start_at - now)
                continue
            try:
                await self._start_engine(cfg, info, mode)
            except Exception as e:
                self._error("engine start", e)
                await self.alert(f"trading engine did not start: {e}", "ERROR", key="exec-start")
                await self._stop_engine(f"start failed: {e}")
                self.note = f"start failed: {e}"
                await self.clock.sleep(60.0)
                continue
            e = self.engine
            assert e is not None and e._stop_event is not None
            await e._stop_event.wait()  # the engine stops itself at session end
            await self._stop_engine(e.stop_reason or "session end")
            if self._restart:
                self._restart = False
                continue
            self._session_done = today

    async def _token(self, platform: str, wait_s: float = 30.0) -> dict | None:
        for _ in range(int(wait_s / 0.2)):
            info = await self.bus.kv_get(topics.KV_TOKEN)
            if info and info.get("platform") == platform and info.get("token"):
                return info
            await asyncio.sleep(0.2)
        return None

    async def _prepare(self) -> tuple[AppConfig, dict, str]:
        s = self.settings
        platform = s.platform.name
        info = await self._token(platform)
        if info is None:
            raise RuntimeError(f"no {platform} access token from the config bot yet")
        path = self.ctx.paths.resolve(s.execution.dhanbot_config)
        if not path.exists():
            raise RuntimeError(f"dhan_bot config not found: {path} (start from config/dhanbot.example.toml)")
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise RuntimeError(f"{path}: TOML syntax error: {e}") from None
        wants_live = isinstance(raw.get("mode"), dict) and raw["mode"].get("mode") == "live"
        if platform == "simulated":
            mode = "live"  # orders go to the simulated exchange; no real money is involved
        else:
            mode = "live" if (s.execution.live and wants_live) else "paper"
        for section, key, value in (("mode", "mode", mode), ("dhan", "auth", "token"), ("telegram", "enabled", False)):
            if isinstance(raw.setdefault(section, {}), dict):
                raw[section][key] = value  # token: the config bot owns logins; alerts go to the team's alert topic
        if platform == "simulated" and isinstance(raw.setdefault("paths", {}), dict):
            raw["paths"]["data_dir"] = str(self.ctx.paths.bot("execution") / "simulated")  # never mix with real data
        env = {**os.environ, "DHAN_CLIENT_ID": str(info["client_id"]), "DHAN_ACCESS_TOKEN": str(info["token"])}
        try:
            cfg = load_dhan_config(data=raw, env=env, env_file=path.parent / ".env",
                                   live_requested=(mode == "live" and platform != "simulated"))
        except ConfigError as e:
            raise RuntimeError(f"dhan_bot config {path}: " + "; ".join(e.problems)) from None
        cfg = dataclasses.replace(cfg, base_dir=str(path.parent.resolve()))
        return cfg, info, mode

    async def _start_engine(self, cfg: AppConfig, info: dict, mode: str) -> None:
        if self._shutdown_task is not None and not self._shutdown_task.done():
            await asyncio.shield(self._shutdown_task)  # the previous engine must release its lock first
        platform = self.settings.platform.name
        endpoints = endpoints_from(info)
        exp = info.get("expires_at")
        need = epoch_at(self.ctx.today(), cfg.session_times()["session_end"]) + 1800
        if platform == "dhan" and exp and float(exp) < need:
            await self.alert("the Dhan token expires before the trading session ends - raise "
                             "platform.token_valid_until so the config bot renews it in time", "WARNING", key="exec-token")
        # Seed the engine's token cache with the config bot's token, so it never picks an older cached one.
        TokenManager(cfg, endpoints, self.clock).save_manual(str(info["token"]))
        engine: Engine | None = None

        def exposure() -> float:
            return engine.positions.gross_exposure_paise() / 100.0 if engine is not None else 0.0

        def pnl() -> float:
            return engine.risk.net_pnl_inr() if engine is not None and engine.risk is not None else 0.0

        def price(o: Order) -> float:
            assert engine is not None
            return engine.risk.reference_price_paise(o)

        guard = self.guard or BudgetGuard(platform, self.clock)
        guard.bind(platform, exposure_fn=exposure, pnl_fn=pnl, price_fn=price)
        engine = Engine(cfg, mode=mode, clock=self.clock, endpoints=endpoints, use_telegram=False,
                        extra_risk_checks=[guard.check], order_listeners=[guard.on_event, self._on_order_event])
        engine.ks.listeners.append(self._on_killswitch)
        self.engine, self.guard = engine, guard
        self.platform_name, self._cid_prefix = platform, cfg.dhan.cid_prefix
        self.exec_mode = "simulated-exchange" if platform == "simulated" else mode
        self._token_version, self._endpoints_info = int(info.get("version", 0)), info.get("endpoints")
        self.note = f"starting ({self.exec_mode} on {platform})"
        await engine.start()
        self.note = f"running ({self.exec_mode} on {platform})"
        self._queue(topics.EXEC_STATE, {"state": engine.ks.state.value, "reason": "engine started",
                                        "mode": self.exec_mode, "platform": platform, "ts": self.clock.time()})
        await self.alert(f"trading engine started: {self.exec_mode.upper()} on {platform}, strategy "
                         f"{engine.strategy.name if engine.strategy else '-'}", "INFO", key="exec-started")

    async def _stop_engine(self, reason: str) -> None:
        e = self.engine
        if e is not None:
            self.engine = None
            e.request_stop(reason)
            self._shutdown_task = asyncio.get_running_loop().create_task(self._shutdown(e, reason))
        t = self._shutdown_task
        if t is not None and not t.done():
            await asyncio.shield(t)  # a cancelled caller must never interrupt a shutdown (flatten, lock release)

    async def _shutdown(self, e: Engine, reason: str) -> None:
        report: dict = {}
        try:
            report = await e.shutdown()
        except Exception as ex:
            self._error("engine shutdown", ex)
        self.note = f"stopped: {reason}"
        await self._forward_alerts(e)
        self._queue(topics.EXEC_STATE, {"state": "STOPPED", "reason": reason, "mode": self.exec_mode,
                                        "platform": self.platform_name, "ts": self.clock.time(),
                                        "report": {k: report.get(k) for k in ("pnl", "trades", "orders", "fills",
                                                                              "risk_rejections") if k in report}})

    # ---- events -------------------------------------------------------------------------------------------------
    def _queue(self, topic: str, data: dict) -> None:
        try:
            self._events.put_nowait((topic, data))
        except asyncio.QueueFull:
            self.counters["dropped_events"] = self.counters.get("dropped_events", 0) + 1

    def _on_killswitch(self, state: TradingState, reason: str) -> None:
        self._queue(topics.EXEC_STATE, {"state": state.value, "reason": reason, "mode": self.exec_mode,
                                        "platform": self.platform_name, "ts": self.clock.time()})

    def _on_order_event(self, ev: OrderEvent) -> None:
        o = ev.order
        if not o.cid:  # refused by the risk rules / budget guard before it was ever sent
            if ev.kind == "rejected":
                self._on_reject(o)
            return
        now = self.clock.time()
        if ev.kind == "leg_update":  # the engine re-reports leg states on every reconcile: publish changes only
            legs = tuple(sorted((k, getattr(v, "value", str(v))) for k, v in (o.meta.get("leg_status") or {}).items()))
            if self._legs_sent.get(o.cid) == legs:
                return
            self._legs_sent[o.cid] = legs
        placed = self._placed.setdefault(o.cid, now)
        if len(self._placed) > 20_000:  # a long-running engine: keep the map bounded
            for k in list(self._placed)[:10_000]:
                self._placed.pop(k, None)
                self._legs_sent.pop(k, None)
        self._queue(topics.EXEC_ORDER, {
            "cid": o.cid, "broker_id": o.broker_id, "symbol": o.instrument.trading_symbol, "segment": o.instrument.segment,
            "side": o.side.value, "qty": o.qty, "price": o.price_paise / 100, "status": o.status.value,
            "intent": o.intent.value, "role": o.role, "filled_qty": o.filled_qty,
            "avg_price": round(o.avg_fill_paise / 100, 4), "reason": o.reason, "event": ev.kind, "updated_ts": now,
            "placed_ts": placed, "platform": self.platform_name})
        f = ev.fill
        if ev.kind == "fill" and f is not None:
            self._queue(topics.EXEC_FILL, {
                "cid": o.cid, "broker_id": o.broker_id, "symbol": o.instrument.trading_symbol,
                "segment": o.instrument.segment, "side": f.side.value, "qty": f.qty, "price": round(f.price_paise / 100, 4),
                "value": round(f.qty * f.price_paise * o.instrument.multiplier / 100, 2), "intent": o.intent.value,
                "leg": f.leg, "role": o.role, "ts": f.ts, "platform": self.platform_name})

    def _on_reject(self, o: Order) -> None:
        rule = (o.reason or "rejected").split(":", 1)[0]
        key, now = (o.instrument.trading_symbol, rule), self.clock.time()
        last = self._rejects.get(key)
        if last is not None and now - last[0] < REJECT_THROTTLE_S:
            last[1] += 1
            return
        count = 1 + (last[1] if last is not None else 0)
        self._rejects[key] = [now, 0]
        self._queue(topics.EXEC_REJECT, {"symbol": key[0], "side": o.side.value, "qty": o.qty, "rule": rule,
                                         "reason": o.reason, "count": count, "budget": rule.startswith("budget"),
                                         "ts": now, "platform": self.platform_name})

    async def _publisher(self) -> None:
        while True:
            topic, data = await self._events.get()
            try:
                await self.publish(topic, data)
            except Exception as e:
                self._error("publish event", e)

    async def _forward_alerts(self, e: Engine) -> None:
        """The engine's warnings and errors (preflight, kill switch, ...) and its day report go to the team's alert
        topic. Routine INFO notes such as each fill are left out: fills are on the Trading page."""
        for a in list(e.alerts):
            k = (a.ts, a.text)
            if k in self._fwd_alerts:
                continue
            self._fwd_alerts.append(k)
            if a.level in ("WARNING", "ERROR") or a.text.startswith("Day report"):
                text = a.text
                if self.exec_mode == "simulated-exchange":  # the engine says [live]: it trades with the simulated exchange
                    text = text.replace("[live]", "[simulated exchange]")
                await self.alert(f"[engine] {text}", a.level if a.level in ("INFO", "WARNING", "ERROR") else "WARNING",
                                 key="exec-day-report" if a.text.startswith("Day report") else "")

    async def publish_state(self) -> None:
        e, now = self.engine, self.clock.time()
        base = {"running": False, "note": self.note, "mode": self.exec_mode or None,
                "platform": self.platform_name or self.settings.platform.name, "cid_prefix": self._cid_prefix,
                "guard": self.guard.usage() if self.guard else None, "ts": now,
                "enabled": self.settings.execution.enabled}
        if e is None or e.oms is None or e.risk is None or e._shutting_down:
            await self.bus.kv_set(topics.KV_EXEC_STATUS, base)
            return
        b = e.positions
        pnl = {"realised": round(b.realised_paise() / 100, 2), "unrealised": round(b.unrealised_paise() / 100, 2),
               "charges": round(b.charges_paise() / 100, 2), "net": round(b.net_paise() / 100, 2),
               "exposure": round(b.gross_exposure_paise() / 100, 2), "open_positions": len(b.open_positions()),
               "positions": [p.to_dict() for p in b.open_positions()], "date": self.ctx.today().isoformat(),
               "mode": self.exec_mode, "platform": self.platform_name}
        if pnl != self._last_pnl or now - self._last_pnl_ts >= 30:
            self._last_pnl, self._last_pnl_ts = pnl, now
            await self.publish(topics.EXEC_PNL, {**pnl, "ts": now})
        status = {**base, **e.status(), "running": True, "mode": self.exec_mode, "engine_mode": e.mode}
        await self.bus.kv_set(topics.KV_EXEC_STATUS, status)
        await self._forward_alerts(e)
        loss = float((self.guard.limits if self.guard else {}).get("max_daily_loss_inr") or 0)
        if loss and (self.guard.limits.get("enabled", True)) and e.ks.state is not TradingState.HALTED:
            net = e.risk.net_pnl_inr()
            if net <= -loss:  # the budget's loss limit halts (and flattens), not just blocks entries
                e.ks.halt(f"daily budget loss limit ₹{loss:,.0f} reached (net ₹{net:,.0f})")

    # ---- inputs -------------------------------------------------------------------------------------------------
    async def on_policy(self, msg: Message) -> None:
        if self.guard is not None:
            self.guard.update_policy(msg.data)

    async def on_external(self, msg: Message) -> None:
        if self.guard is not None:
            self.guard.update_external(msg.data)

    async def on_token(self, msg: Message) -> None:
        e = self.engine
        if e is None or e.tokens is None:
            return
        info = await self.bus.kv_get(topics.KV_TOKEN)
        if not info or info.get("platform") != self.platform_name or not info.get("token"):
            return
        if int(info.get("version", 0)) == self._token_version:
            return
        self._token_version = int(info.get("version", 0))
        if info.get("endpoints") != self._endpoints_info:  # the simulated exchange restarted elsewhere
            if not self._busy(e):
                self._restart = True
                self.spawn(self._stop_engine("simulated exchange moved - restarting"), name="restart")
            return
        if info["token"] != e.tokens.token:
            e.tokens.save_manual(str(info["token"]))  # updates the REST client via the token listener
            for conn in (e.feed, e.order_ws):
                if conn is not None:
                    await conn.recycle()  # sockets reconnect with the new token

    # ---- commands -----------------------------------------------------------------------------------------------
    async def cmd_status(self, data: dict) -> dict:
        e = self.engine
        return {"running": e is not None, "note": self.note, "mode": self.exec_mode or None,
                "platform": self.platform_name or None, "status": e.status() if e is not None and e.oms else None,
                "guard": self.guard.usage() if self.guard else None}

    async def cmd_control(self, data: dict) -> dict:
        action = str(data.get("action") or "status")
        if action not in ("status", "pause", "resume", "flatten", "kill"):
            return {"done": False, "error": f"unknown action {action!r} (status, pause, resume, flatten, kill)"}
        e = self.engine
        if e is None or e.oms is None:
            return {"done": False, "error": f"trading engine not running ({self.note})"}
        handlers: dict[str, Any] = e.command_handlers()
        out = handlers[action]({"source": str(data.get("source") or "dashboard")})
        if asyncio.iscoroutine(out):
            out = await out
        await self.publish_state()
        return {"done": True, "result": out}
