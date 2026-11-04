"""Where notifications go: Telegram (instant, with commands) and email (summary and errors).

Telegram reuses dhan_bot's client:
- a send queue with a per-minute cap;
- long polling (no inbound port);
- it obeys only the configured chat;
- `/flatten` and `/kill` need `/confirm` within 30 s.

Only one process may poll a bot token. That is the notifier: the trading engine's own Telegram stays off
inside the team.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

import aiohttp
from dhanbot.notify.telegram import API, Telegram

from ...core.settings import NotifyCfg

TEAM_HELP = (
    "/status - bots, trading engine, budget\n"
    "/pnl - today's profit and loss\n"
    "/positions - open positions\n"
    "/budget - daily budget used and left\n"
    "/report - latest AI report\n"
    "/ai - run an AI analysis now\n"
    "/pause - stop new entries\n"
    "/resume - allow entries again\n"
    "/flatten - exit all engine positions (needs /confirm)\n"
    "/kill - halt trading for today + flatten (needs /confirm)"
)


class TeamTelegram(Telegram):
    """dhan_bot's client with the team's command list."""

    async def handle_text(self, text: str) -> str:
        cmd = text.strip().split(" ", 1)[0].lstrip("/").split("@", 1)[0].lower()
        if cmd in ("help", "start"):
            return TEAM_HELP
        if cmd and cmd not in self.handlers and cmd != "confirm":
            return f"unknown command /{cmd}\n\n{TEAM_HELP}"
        return await super().handle_text(text)

    async def send_now(self, text: str) -> dict:
        """Send immediately and return Telegram's answer (for `bots notify test`); works before start() too."""
        own = self._session is None
        if own:
            self._session = aiohttp.ClientSession()
        try:
            return await self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:3900],
                                                    "disable_web_page_preview": True})
        finally:
            if own:
                await self._session.close()
                self._session = None


class EmailSender:
    """SMTP with STARTTLS (port 587) or SSL (465). Runs in a thread so the event loop never blocks."""

    def __init__(self, host: str, port: int, user: str, password: str, sender: str, to: list[str],
                 use_ssl: bool = False, timeout: float = 20.0) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender, self.to, self.use_ssl, self.timeout = sender or user, to, use_ssl, timeout
        self.sent = 0

    def _send(self, subject: str, text: str, html: str | None) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.sender, ", ".join(self.to)
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        ctx = ssl.create_default_context()
        if self.use_ssl:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx) as s:
                if self.user:
                    s.login(self.user, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                s.starttls(context=ctx)
                if self.user:
                    s.login(self.user, self.password)
                s.send_message(msg)
        self.sent += 1

    async def send(self, subject: str, text: str, html: str | None = None) -> None:
        await asyncio.to_thread(self._send, subject, text, html)


def telegram_from_env(env: dict, handlers: dict, commands: bool) -> tuple[TeamTelegram | None, str]:
    token, chat = env.get("TELEGRAM_BOT_TOKEN", "").strip(), env.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return None, "Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see .env.example)"
    return TeamTelegram(token, chat, handlers, commands=commands, api=env.get("TELEGRAM_API_BASE") or API), ""


def email_from_env(env: dict) -> tuple[EmailSender | None, str]:
    host, to = env.get("SMTP_HOST", "").strip(), [x.strip() for x in env.get("EMAIL_TO", "").split(",") if x.strip()]
    if not (host and to):
        return None, "email: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and EMAIL_TO in .env (see .env.example)"
    use_ssl = env.get("SMTP_SSL", "").strip().lower() in ("1", "true", "yes")
    port = int(env.get("SMTP_PORT") or (465 if use_ssl else 587))
    return EmailSender(host, port, env.get("SMTP_USER", ""), env.get("SMTP_PASSWORD", ""),
                       env.get("EMAIL_FROM", "") or env.get("SMTP_USER", ""), to, use_ssl), ""


def open_channels(env: dict, n: NotifyCfg, handlers: dict, email_factory=None) -> tuple[TeamTelegram | None, object, dict]:
    """The channels the settings and ``.env`` allow (not started), plus why each missing one is off."""
    if not n.enabled:
        off = "notifications are switched off (notify.enabled = false)"
        return None, None, {"telegram": off, "email": off}
    why: dict[str, str] = {}
    tg = em = None
    if n.telegram:
        tg, w = telegram_from_env(env, handlers, n.commands)
        if tg is None:
            why["telegram"] = w
    else:
        why["telegram"] = "Telegram is switched off (notify.telegram = false)"
    if n.email:
        em, w = (email_factory or email_from_env)(env)
        if em is None:
            why["email"] = w
    else:
        why["email"] = "email is switched off (notify.email = false)"
    return tg, em, why


async def send_tests(tg: TeamTelegram | None, em, why: dict) -> dict[str, str]:
    """One test message on each channel: "sent", "off - <why>" or "failed: <error>"."""
    out: dict[str, str] = {}
    if tg is not None:
        try:
            r = await tg.send_now("✅ trading-bots: Telegram works.")
            out["telegram"] = "sent" if r.get("ok") else f"failed: Telegram said {r.get('description')}"
        except Exception as e:
            out["telegram"] = f"failed: {type(e).__name__}: {e}"
    else:
        out["telegram"] = "off - " + why.get("telegram", "")
    if em is not None:
        try:
            await em.send("[trading-bots] test", "Email works. Alerts and the daily summary will arrive here.")
            out["email"] = "sent"
        except Exception as e:
            out["email"] = f"failed: {type(e).__name__}: {e}"
    else:
        out["email"] = "off - " + why.get("email", "")
    return out
