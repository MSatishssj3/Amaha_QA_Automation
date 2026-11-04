"""Notifier: alert filtering and dedupe, quiet hours, Telegram commands (only from your chat, /confirm for
flatten), the daily summary, the test command and missing credentials, against a fake Telegram API and a
fake email sender."""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from aiohttp import web

from tradingbots.bots.notifier.bot import NotifierBot, in_quiet_hours
from tradingbots.core import topics

from .helpers import make_settings, wait_for
from .team import Team

CHAT = "4242"


class FakeTelegramAPI:
    """Just enough of the Bot API: sendMessage and getUpdates (long poll)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.pending: list[dict] = []
        self._n = 1

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/bot{token}/sendMessage", self.h_send)
        app.router.add_post("/bot{token}/getUpdates", self.h_updates)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        await self.runner.cleanup()

    async def h_send(self, req: web.Request) -> web.Response:
        self.sent.append(await req.json())
        return web.json_response({"ok": True, "result": {"message_id": len(self.sent)}})

    async def h_updates(self, req: web.Request) -> web.Response:
        offset = int((await req.json()).get("offset") or 0)
        items = [u for u in self.pending if u["update_id"] >= offset]
        if not items:
            await asyncio.sleep(0.1)
        return web.json_response({"ok": True, "result": items})

    def push(self, text: str, chat: str = CHAT) -> None:
        self.pending.append({"update_id": self._n, "message": {"chat": {"id": int(chat)}, "text": text}})
        self._n += 1

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


class FakeEmail:
    def __init__(self) -> None:
        self.mails: list[tuple[str, str, str | None]] = []

    async def send(self, subject: str, text: str, html: str | None = None) -> None:
        self.mails.append((subject, text, html))


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    for k in list(os.environ):  # the process environment overrides .env: keep the developer's own out
        if k.startswith(("TELEGRAM_", "SMTP_", "EMAIL_")):
            monkeypatch.delenv(k)


@pytest.fixture
async def tg(tmp_path, monkeypatch):
    api = FakeTelegramAPI()
    url = await api.start()
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID={CHAT}\nTELEGRAM_API_BASE={url}\n")
    mail = FakeEmail()
    monkeypatch.setattr(NotifierBot, "email_factory", staticmethod(lambda env: (mail, "")))
    yield api, mail
    await api.stop()


def notify_settings(tmp, **kw):
    return make_settings(tmp, notify={"quiet_hours": [], **kw})


async def alert(c, level: str, text: str, key: str = "", source: str = "budget") -> None:
    await c.publish(topics.ALERT, {"level": level, "source": source, "text": text, "key": key, "ts": time.time()})


def test_quiet_hours_window():
    assert in_quiet_hours("23:30", ["22:00", "07:00"]) and in_quiet_hours("06:59", ["22:00", "07:00"])
    assert not in_quiet_hours("07:00", ["22:00", "07:00"]) and not in_quiet_hours("12:00", ["22:00", "07:00"])
    assert in_quiet_hours("13:00", ["12:00", "14:00"]) and not in_quiet_hours("14:00", ["12:00", "14:00"])
    assert not in_quiet_hours("03:00", [])


async def test_alerts_are_filtered_deduplicated_and_emailed_by_level(tmp_path, tg):
    api, mail = tg
    async with Team(tmp_path, ["notifier"], settings=notify_settings(tmp_path)) as team:
        c = team.client
        await alert(c, "WARNING", "daily budget: New entries today at 83%", key="budget:daily_capital@80")
        await alert(c, "WARNING", "daily budget: New entries today at 83%", key="budget:daily_capital@80")  # repeat
        await alert(c, "INFO", "money tables refreshed", key="money-refresh")  # INFO, not on the list
        await alert(c, "INFO", "trading engine started: PAPER on dhan", key="exec-started", source="execution")
        await alert(c, "ERROR", "[engine] trading HALTED: daily loss limit hit", source="execution")
        await alert(c, "WARNING", "email could not be sent", key="notify-email", source="notifier")  # own: never echoed
        assert await wait_for(lambda: len(api.sent) >= 3 and len(mail.mails) >= 1, 10), api.texts()
        await asyncio.sleep(0.5)
        texts = api.texts()
        assert texts == ["⚠️ budget: daily budget: New entries today at 83%",
                         "ℹ️ execution: trading engine started: PAPER on dhan",
                         "⛔ execution: [engine] trading HALTED: daily loss limit hit"]
        assert len(mail.mails) == 1 and mail.mails[0][0].startswith("[trading-bots] ERROR: [engine] trading HALTED")
        st = await c.request(topics.cmd("notifier", "status"), {})
        assert st["telegram"] == "on" and st["email"] == "on" and st["sent"]["suppressed"] == 1


async def test_quiet_hours_hold_back_warnings_but_not_errors(tmp_path, tg):
    api, _ = tg
    s = make_settings(tmp_path, notify={"quiet_hours": ["00:00", "23:59"]})  # quiet all day
    async with Team(tmp_path, ["notifier"], settings=s) as team:
        await alert(team.client, "WARNING", "near a limit", key="w1")
        await alert(team.client, "ERROR", "token expired", key="e1", source="config")
        assert await wait_for(lambda: len(api.sent) >= 1, 10)
        await asyncio.sleep(0.4)
        assert api.texts() == ["⛔ config: token expired"]


async def test_telegram_commands_only_from_your_chat_and_flatten_needs_confirm(tmp_path, tg):
    api, _ = tg
    async with Team(tmp_path, ["notifier"], settings=notify_settings(tmp_path)) as team:
        c = team.client
        await c.kv_set(topics.KV_BUDGET_USAGE, {
            "date": "2026-11-05", "limits": [{"rule": "daily_capital", "label": "New entries today", "unit": "inr",
                                              "used": 82193.0, "limit": 120000.0, "remaining": 37807.0, "pct": 68.5}],
            "external": {"turnover_inr": 7921.0}, "budget_rejects": 1, "total": {"entries": 7}})
        fake_exec = team.bus_factory("fake-execution")
        await fake_exec.connect()
        topic = topics.cmd("execution", "control")
        await fake_exec.ensure_group("fake-execution", [topic])
        asked: list[dict] = []

        async def execution() -> None:
            while True:
                for m in await fake_exec.consume("fake-execution", [topic], block_s=0.5):
                    corr = m.data.pop("_corr", None)
                    asked.append(m.data)
                    await fake_exec.send_reply(corr, {"ok": True, "done": True, "result": {"flattened": True}})
                    await fake_exec.ack("fake-execution", [m])

        task = asyncio.create_task(execution())
        try:
            api.push("/budget", chat="999")  # a stranger: ignored
            api.push("/budget")
            assert await wait_for(lambda: any("New entries today" in t for t in api.texts()), 10), api.texts()
            assert "₹82,193 of ₹1,20,000 (68%)" in api.texts()[-1] and "blocked entries: 1" in api.texts()[-1]
            assert len(api.texts()) == 1  # nothing for the stranger
            api.push("/flatten")
            assert await wait_for(lambda: any("/confirm" in t for t in api.texts()), 10)
            assert not asked  # not before /confirm
            api.push("/confirm")
            assert await wait_for(lambda: any("/flatten done" in t for t in api.texts()), 15), api.texts()
            assert asked == [{"action": "flatten", "source": "telegram"}]
            api.push("/help")
            api.push("/what")
            assert await wait_for(lambda: any(t.startswith("unknown command /what") for t in api.texts()), 10)
            assert any(t.startswith("/status - bots") for t in api.texts())
        finally:
            task.cancel()
            await fake_exec.close()


async def test_daily_summary_and_test_command(tmp_path, tg):
    api, mail = tg
    async with Team(tmp_path, ["notifier"], settings=notify_settings(tmp_path)) as team:
        c = team.client
        day = team.bots["notifier"].ctx.today().isoformat()
        now = team.clock.time()
        await c.publish(topics.EXEC_PNL, {"date": day, "net": -329.5, "realised": -24.5, "unrealised": 0.0,
                                          "charges": 305.0, "open_positions": 0, "positions": []})
        await c.kv_set(topics.KV_BUDGET_USAGE, {
            "date": day, "limits": [{"rule": "daily_capital", "label": "New entries today", "unit": "inr",
                                     "used": 99408.0, "limit": 120000.0, "remaining": 20592.0, "pct": 82.8}],
            "external": {"turnover_inr": 7921.0}, "budget_rejects": 1, "total": {"entries": 12}})
        await c.publish(topics.JOURNAL_UPDATED, {"day": day, "today": {"trades": 4, "wins": 3, "win_rate": 75.0,
                                                                       "net_pnl": 1234.0, "expectancy": 308.5}})
        await c.publish(topics.AI_REPORT, {"report_id": "r1", "status": "ok", "headline": "Large caps steady",
                                           "ts": now, "kind": "post_close", "served_by": "claude-opus-5-5"})
        await c.publish(topics.ALERT, {"level": "ERROR", "source": "budget", "key": "x",
                                       "text": "Entries today limit reached - 12 of 12", "ts": now})
        r = await c.request(topics.cmd("notifier", "summary"), {}, timeout=10)
        assert r["sent"] and r["email"]
        t = r["text"]
        assert t.startswith("📊 Daily summary") and "Trading engine: net ▼ -₹330" in t
        assert "Daily budget: ₹99,408 of ₹1,20,000 used (83%), 12 entries, 1 blocked" in t
        assert "AI: Large caps steady" in t and "Alerts: 0 warnings, 1 error" in t
        assert "Closed trades: 4 (engine + yours), 3 won (75%), net ▲ +₹1,234, expectancy +₹308 per trade" in t
        assert await wait_for(lambda: any(x.startswith("📊 Daily summary") for x in api.texts()), 10)
        subject, body, html = mail.mails[-1]
        assert subject.startswith("[trading-bots] Daily summary") and "<table>" in html and "Large caps steady" in html
        test = await c.request(topics.cmd("notifier", "test"), {}, timeout=10)
        assert test["telegram"] == "sent" and test["email"] == "sent"
        assert "✅ trading-bots: Telegram works." in api.texts()


async def test_missing_credentials_keep_channels_off_and_say_why(tmp_path, monkeypatch):
    monkeypatch.setattr(NotifierBot, "email_factory", None)
    async with Team(tmp_path, ["notifier"], settings=notify_settings(tmp_path)) as team:
        st = await team.client.request(topics.cmd("notifier", "status"), {})
        assert st["telegram"] == "off" and st["email"] == "off"
        assert any("TELEGRAM_BOT_TOKEN" in p for p in st["problems"]) and any("SMTP_HOST" in p for p in st["problems"])
        test = await team.client.request(topics.cmd("notifier", "test"), {})
        assert test["telegram"].startswith("off - Telegram: set") and test["email"].startswith("off - email: set")
        await alert(team.client, "ERROR", "nobody hears this")  # no channel: nothing breaks
        await asyncio.sleep(0.3)
        assert team.bots["notifier"].status == "ok"
