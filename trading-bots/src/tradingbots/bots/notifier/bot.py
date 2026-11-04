"""Notifier bot: what needs your attention reaches your phone and inbox.

* **Telegram:**
  - Alerts at or above `notify.min_level`, plus selected INFO alerts (`notify.info_keys`).
  - The same alert is sent at most once per `dedupe_minutes`. During `quiet_hours` only ERROR alerts go out.
  - Commands from your chat only: `/status`, `/pnl`, `/positions`, `/budget`, `/report`, `/ai`, `/pause`,
    `/resume`, `/flatten` and `/kill`. The last two need `/confirm`.
* **Email:** the daily summary, plus alerts at `notify.email_min_level` and above (default ERROR).
* **Daily summary** at `notify.daily_summary_at` on trading days: the engine's P&L, the daily budget, money
  headlines, the AI headline, today's warnings and bot health.

Credentials live in `.env`. A channel without credentials stays off and says why (dashboard → Settings →
Notifications). `cmd.notifier.test` sends a test message on every channel.
"""

from __future__ import annotations

import html
import time
from typing import Any, Callable

from dhanbot.clock import ist_datetime

from ...core import topics
from ...core.bot import Bot
from ...core.bus import BusTimeout, Message
from ...core.fmt import arrow, inr, pct, plural
from ...core.secrets import load_env
from ...core.settings import Settings
from ...core.tables_io import TableReader
from ...supervisor import KV_SUPERVISOR
from .channels import open_channels, send_tests

LEVELS = {"INFO": 0, "WARNING": 1, "ERROR": 2}
ICON = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "⛔"}


def in_quiet_hours(hhmm: str, window: list[str]) -> bool:
    """``window`` = [from, to] in HH:MM; it may cross midnight (e.g. 22:00 -> 07:00)."""
    if len(window) != 2:
        return False
    start, end = window
    return start <= hhmm < end if start < end else (hhmm >= start or hhmm < end)


class NotifierBot(Bot):
    name = "notifier"
    email_factory: Callable[[dict], tuple[Any, str]] | None = None  # tests inject a fake sender here

    def configure(self) -> None:
        self.on(topics.ALERT, self.on_alert)
        self.on(topics.AI_REPORT, self.on_ai_report)
        self.command("test", self.cmd_test)
        self.command("status", self.cmd_status)
        self.command("summary", self.cmd_summary)
        self.daily(lambda: self.settings.notify.daily_summary_at, self.daily_summary, name="daily-summary")
        self.telegram = None
        self.email = None
        self.problems: list[str] = []
        self.why: dict[str, str] = {}
        self.sent = {"telegram": 0, "email": 0, "suppressed": 0, "failed": 0}
        self._seen: dict[str, float] = {}
        self._ai_wanted = False

    def details(self) -> dict:
        return {"telegram": "on" if self.telegram else "off", "email": "on" if self.email else "off",
                "problems": self.problems, "why": dict(self.why), "sent": dict(self.sent)}

    async def setup(self) -> None:
        self.reader = TableReader(self.ctx.paths.tables)
        await self._open()

    async def teardown(self) -> None:
        await self._close()

    async def on_settings(self, old: Settings, new: Settings) -> None:
        # Levels, quiet hours and the summary time are read live; only a channel switch reopens the channels
        # (restarting the Telegram poller could replay an unconfirmed command).
        def channels(s: Settings) -> tuple:
            return s.notify.enabled, s.notify.telegram, s.notify.email, s.notify.commands

        if channels(old) != channels(new):
            await self._close()
            await self._open()

    async def _open(self) -> None:
        env = load_env(self.ctx.paths.base)  # also registers the tokens for log redaction
        tg, self.email, self.why = open_channels(env, self.settings.notify, self._tg_commands(), type(self).email_factory)
        self.problems = list(dict.fromkeys(self.why.values()))
        if tg is not None:
            await tg.start()
            self.telegram = tg

    async def _close(self) -> None:
        tg, self.telegram, self.email = self.telegram, None, None
        if tg is not None:
            await tg.stop()

    # ---- alerts -----------------------------------------------------------------------------------------------
    def _wanted(self, level: str, key: str) -> bool:
        n = self.settings.notify
        if LEVELS.get(level, 1) >= LEVELS[n.min_level]:
            return True
        return level == "INFO" and any(k and key.startswith(k) for k in n.info_keys)

    async def on_alert(self, msg: Message) -> None:
        a = msg.data
        if a.get("source") == self.name:  # never echo our own alerts (e.g. "email failed")
            return
        level = str(a.get("level") or "INFO").upper()
        key, text = str(a.get("key") or ""), str(a.get("text") or "")
        if not self._wanted(level, key):
            return
        n, now = self.settings.notify, self.clock.time()
        if LEVELS.get(level, 1) < LEVELS["ERROR"] and in_quiet_hours(f"{ist_datetime(now):%H:%M}", n.quiet_hours):
            self.sent["suppressed"] += 1
            return
        dk = f"{level}:{key or text[:120]}"  # per level: a WARNING never hides a later ERROR
        last = self._seen.get(dk)
        if last is not None and now - last < n.dedupe_minutes * 60:
            self.sent["suppressed"] += 1
            return
        self._seen[dk] = now
        if len(self._seen) > 2000:
            for k in sorted(self._seen, key=self._seen.get)[:1000]:
                self._seen.pop(k, None)
        line = f"{ICON.get(level, '•')} {a.get('source')}: {text}"
        if self.telegram is not None:
            self.telegram.send(line)
            self.sent["telegram"] += 1
        if self.email is not None and LEVELS.get(level, 1) >= LEVELS[n.email_min_level]:
            when = f"{ist_datetime(float(a.get('ts') or now)):%d %b %Y %H:%M:%S} IST"
            await self._email(f"[trading-bots] {level}: {text[:90]}", f"{when}\n{a.get('source')}: {text}\n")

    async def on_ai_report(self, msg: Message) -> None:
        r = msg.data
        if self._ai_wanted and self.telegram is not None:  # an analysis you asked for from Telegram
            self._ai_wanted = False
            if r.get("status") == "ok":
                self.telegram.send(f"🧠 AI report: {r.get('headline')}\n(open the dashboard's AI insights page for detail)")
            else:
                self.telegram.send(f"🧠 AI analysis did not complete: {r.get('error') or r.get('status')}")

    async def _email(self, subject: str, text: str, html_body: str | None = None) -> bool:
        if self.email is None:
            return False
        try:
            await self.email.send(subject, text, html_body)
            self.sent["email"] += 1
            return True
        except Exception as e:
            self.sent["failed"] += 1
            self._error("email", e)
            await self.alert(f"email could not be sent: {type(e).__name__}: {e}", "WARNING", key="notify-email")
            return False

    # ---- Telegram commands ---------------------------------------------------------------------------------------
    def _tg_commands(self) -> dict:
        def control(action: str):
            async def fn(req: dict) -> dict:
                return await self._tg_control(action)
            return fn

        return {"status": self._tg_status, "pnl": self._tg_pnl, "positions": self._tg_positions,
                "budget": self._tg_budget, "report": self._tg_report, "ai": self._tg_ai,
                **{a: control(a) for a in ("pause", "resume", "flatten", "kill")}}

    async def _ask(self, bot: str, action: str, data: dict, timeout: float = 10.0) -> dict:
        try:
            return await self.bus.request(topics.cmd(bot, action), data, timeout=timeout)
        except BusTimeout:
            return {"ok": False, "error": f"the {bot} bot did not answer - is it running?"}

    async def _bot_health(self) -> tuple[int, int, list[str]]:
        now = time.time()
        sup = await self.bus.kv_get(KV_SUPERVISOR) or {}
        if sup.get("bots") and now - float(sup.get("ts") or 0) < 30:
            rows = [b for b in sup["bots"] if b.get("kind") == "bot"]
            bad = [f"{b['name']} ({b['state']})" for b in rows if b["state"] != "running"]
            return len(rows) - len(bad), len(rows), bad
        names = [k[3:] for k in await self.bus.kv_keys("hb:")]
        hbs = {n: await self.bus.kv_get(topics.kv_heartbeat(n)) for n in names}
        bad = [n for n, hb in hbs.items() if not hb or now - float(hb.get("ts") or 0) > 30 or hb.get("status") != "ok"]
        return len(hbs) - len(bad), len(hbs), bad

    async def _tg_status(self, req: dict) -> dict:
        ok, total, bad = await self._bot_health()
        ex = await self.bus.kv_get(topics.KV_EXEC_STATUS) or {}
        u = await self.bus.kv_get(topics.KV_BUDGET_USAGE) or {}
        lines = [f"Bots: {ok}/{total} healthy" + (f" - check {', '.join(bad)}" if bad else "")]
        if ex.get("running"):
            lines.append(f"Engine: {ex.get('mode')} · {ex.get('state')} · net {arrow(ex.get('net_pnl'))} "
                         f"{inr(ex.get('net_pnl'), signed=True)} · {plural(ex.get('open_positions', 0), 'open position')}")
        else:
            lines.append(f"Engine: not running ({ex.get('note') or 'idle'})")
        dc = next((i for i in u.get("limits", []) if i["rule"] == "daily_capital"), None)
        if dc:
            lines.append(f"Budget: {dc['pct']:.0f}% used, {inr(dc['remaining'])} left, "
                         f"{u.get('budget_rejects', 0)} entries blocked")
        return {"text": "\n".join(lines)}

    async def _tg_pnl(self, req: dict) -> dict:
        ex = await self.bus.kv_get(topics.KV_EXEC_STATUS) or {}
        if not ex.get("running"):
            return {"text": f"The trading engine is not running ({ex.get('note') or 'idle'})."}
        return {"text": f"Net {arrow(ex.get('net_pnl'))} {inr(ex.get('net_pnl'), signed=True)} "
                        f"(realised {inr(ex.get('realised'), signed=True)}, unrealised "
                        f"{inr(ex.get('unrealised'), signed=True)}, charges {inr(ex.get('charges'))}) · {ex.get('mode')}"}

    async def _tg_positions(self, req: dict) -> dict:
        recent = await self.bus.recent(topics.EXEC_PNL, 1)
        pos = (recent[-1].data.get("positions") or []) if recent else []
        if not pos:
            return {"text": "No open positions."}
        return {"text": "\n".join(f"{p.get('symbol')}: {p.get('qty')} @ {p.get('avg')} (ltp {p.get('ltp')}, net "
                                  f"{inr(p.get('net'), signed=True)})" for p in pos[:20])}

    async def _tg_budget(self, req: dict) -> dict:
        u = await self.bus.kv_get(topics.KV_BUDGET_USAGE) or {}
        if not u:
            return {"text": "The budget bot has not reported yet."}
        lines = []
        for it in u.get("limits", [])[:8]:
            used = f"{it['used']:.0f} of {it['limit']:.0f}" if it["unit"] == "count" else \
                f"{inr(it['used'])} of {inr(it['limit'])}"
            flag = " ⛔" if it["pct"] >= 100 else (" ⚠️" if it["pct"] >= 80 else "")
            lines.append(f"{it['label']}: {used} ({it['pct']:.0f}%){flag}")
        ext = u.get("external") or {}
        lines.append(f"Your own trades today: {inr(ext.get('turnover_inr'))} · blocked entries: "
                     f"{u.get('budget_rejects', 0)}")
        return {"text": "\n".join(lines)}

    async def _tg_report(self, req: dict) -> dict:
        recent = [m.data for m in await self.bus.recent(topics.AI_REPORT, 10) if m.data.get("status") == "ok"]
        if not recent:
            return {"text": "No AI report yet. Send /ai to run one."}
        r = recent[-1]
        return {"text": f"🧠 {r.get('headline')}\n{str(r.get('kind', '')).replace('_', ' ')} · "
                        f"{ist_datetime(float(r.get('ts') or 0)):%d %b %H:%M} · {r.get('served_by')}"}

    async def _tg_ai(self, req: dict) -> dict:
        r = await self._ask("ai_analyst", "run", {"kind": "on_demand"})
        if r.get("started"):
            self._ai_wanted = True
            return {"text": "Analysis started - the headline will arrive here when it is ready."}
        return {"text": f"Could not start: {r.get('reason') or r.get('error')}"}

    async def _tg_control(self, action: str) -> dict:
        r = await self._ask("execution", "control", {"action": action, "source": "telegram"}, timeout=45)
        if not r.get("ok") or not r.get("done"):
            return {"text": f"/{action} not done: {r.get('error') or 'no answer'}"}
        res = r.get("result") or {}
        state = res.get("state")
        extra = " (positions flattened)" if res.get("flattened") else ""
        return {"text": f"/{action} done{extra}" + (f" - trading is now {state}" if state else "")}

    # ---- daily summary ---------------------------------------------------------------------------------------------
    async def build_summary(self) -> tuple[str, str]:
        day = self.ctx.today()
        d = day.isoformat()

        def today(ts: Any) -> bool:
            return bool(ts) and ist_datetime(float(ts)).date() == day

        pnl = next((m.data for m in reversed(await self.bus.recent(topics.EXEC_PNL, 20)) if m.data.get("date") == d),
                   None)
        u = await self.bus.kv_get(topics.KV_BUDGET_USAGE) or {}
        try:
            ms = {r["metric"]: r.get("value") for r in self.reader.table("money_summary").to_dicts()}
        except Exception:
            ms = {}
        reps = [m.data for m in await self.bus.recent(topics.AI_REPORT, 20) if today(m.data.get("ts"))
                and m.data.get("status") == "ok"]
        alerts = [m.data for m in await self.bus.recent(topics.ALERT, 500) if today(m.data.get("ts"))
                  and m.data.get("level") in ("WARNING", "ERROR")]
        ok, total, bad = await self._bot_health()
        rows: list[tuple[str, str]] = []
        if pnl:
            rows.append(("Trading engine", f"net {arrow(pnl.get('net'))} {inr(pnl.get('net'), signed=True)} (realised "
                                           f"{inr(pnl.get('realised'), signed=True)}, charges {inr(pnl.get('charges'))}), "
                                           f"{plural(pnl.get('open_positions', 0), 'open position')}"))
        else:
            rows.append(("Trading engine", "did not trade today"))
        dc = next((i for i in u.get("limits", []) if i["rule"] == "daily_capital"), None) if u.get("date") == d else None
        if dc:
            ext = u.get("external") or {}
            rows.append(("Daily budget", f"{inr(dc['used'])} of {inr(dc['limit'])} used ({dc['pct']:.0f}%), "
                                         f"{plural(u['total']['entries'], 'entry', 'entries')}, "
                                         f"{u.get('budget_rejects', 0)} blocked, your own trades "
                                         f"{inr(ext.get('turnover_inr'))}"))
        if ms:
            rows.append(("Money", f"net worth {inr(ms.get('net_worth'), compact=True)}, realised today "
                                  f"{inr(ms.get('realized_pnl_today'), signed=True)}, charges this month "
                                  f"{inr(ms.get('charges_mtd'))}" + (f", XIRR {pct(ms.get('xirr'))}"
                                                                     if ms.get("xirr") is not None else "")))
        rows.append(("AI", reps[-1].get("headline") if reps else "no report today"))
        errs = sum(a.get("level") == "ERROR" for a in alerts)
        rows.append(("Alerts", f"{plural(len(alerts) - errs, 'warning')}, {plural(errs, 'error')}"))
        rows.append(("Bots", f"{ok}/{total} healthy" + (f" - check {', '.join(bad)}" if bad else "")))
        title = f"Daily summary {day:%a %d %b %Y}"
        text = "\n".join([f"📊 {title}"] + [f"{k}: {v}" for k, v in rows])
        if alerts:
            text += "\n\nLatest:\n" + "\n".join(f"- {a.get('source')}: {a.get('text')}" for a in alerts[-3:])
        cells = "".join(f"<tr><td style='padding:4px 12px 4px 0;color:#52514e'>{html.escape(k)}</td>"
                        f"<td style='padding:4px 0'>{html.escape(v)}</td></tr>" for k, v in rows)
        html_body = (f"<div style='font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#0b0b0b'>"
                     f"<h2 style='margin:0 0 12px 0'>{html.escape(title)}</h2><table>{cells}</table>"
                     + ("<p style='margin-top:12px'><b>Latest alerts</b><br>"
                        + "<br>".join(html.escape(f"{a.get('source')}: {a.get('text')}") for a in alerts[-5:]) + "</p>"
                        if alerts else "")
                     + "<p style='color:#898781;font-size:12px'>trading-bots · decision support, not advice</p></div>")
        return text, html_body

    async def daily_summary(self) -> dict:
        text, html_body = await self.build_summary()
        if self.telegram is not None:
            self.telegram.send(text)
            self.sent["telegram"] += 1
        emailed = await self._email(f"[trading-bots] {text.splitlines()[0].lstrip('📊 ')}", text, html_body)
        return {"sent": True, "telegram": self.telegram is not None, "email": emailed, "text": text}

    # ---- commands ---------------------------------------------------------------------------------------------------
    async def cmd_status(self, data: dict) -> dict:
        return self.details()

    async def cmd_summary(self, data: dict) -> dict:
        return await self.daily_summary()

    async def cmd_test(self, data: dict) -> dict:
        out = await send_tests(self.telegram, self.email, self.why)
        for ch in ("telegram", "email"):
            if out[ch] == "sent":
                self.sent[ch] += 1
            elif out[ch].startswith("failed"):
                self.sent["failed"] += 1
        return out
