"""Optional Telegram alerts + remote control.

* Outgoing alerts go through a queue (never blocks the hot path) with a per-minute cap.
* Commands use Bot API long-polling (``getUpdates``) - the server needs no inbound port.
* Only messages from the configured ``TELEGRAM_CHAT_ID`` are obeyed.
* ``/flatten`` and ``/kill`` require ``/confirm`` within 30 seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable

import aiohttp
import orjson

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
DANGEROUS = {"flatten", "kill"}
HELP = ("/status - state, P&L, connections\n/pnl - P&L breakdown\n/positions - open positions\n"
        "/orders - working orders\n/pause - stop new entries\n/resume - allow entries again\n"
        "/flatten - exit all bot positions (needs /confirm)\n/kill - HALT for the day + flatten (needs /confirm)")


class Telegram:
    def __init__(self, token: str, chat_id: str, handlers: dict[str, Callable[[dict], Awaitable[dict] | dict]],
                 max_per_min: int = 20, commands: bool = True, api: str = API) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.handlers = handlers
        self.max_per_min = max_per_min
        self.commands = commands
        self.api = api
        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._sent: deque[float] = deque()
        self._pending_confirm: tuple[str, float] | None = None
        self._tasks: list[asyncio.Task] = []
        self._session: aiohttp.ClientSession | None = None
        self.dropped = 0

    def send(self, text: str) -> None:
        try:
            self._q.put_nowait(text[:3900])
        except asyncio.QueueFull:
            self.dropped += 1

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._tasks.append(asyncio.create_task(self._sender()))
        if self.commands:
            self._tasks.append(asyncio.create_task(self._poller()))

    async def stop(self, drain_s: float = 3.0) -> None:
        deadline = time.monotonic() + drain_s
        while not self._q.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._session:
            await self._session.close()

    async def _call(self, method: str, payload: dict, timeout: float = 10) -> dict:
        assert self._session is not None
        async with self._session.post(f"{self.api}/bot{self.token}/{method}", data=orjson.dumps(payload),
                                      headers={"Content-Type": "application/json"},
                                      timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return orjson.loads(await r.read() or b"{}")

    async def _sender(self) -> None:
        while True:
            text = await self._q.get()
            now = time.monotonic()
            while self._sent and now - self._sent[0] > 60:
                self._sent.popleft()
            if len(self._sent) >= self.max_per_min:
                await asyncio.sleep(60 - (now - self._sent[0]))
            try:
                await self._call("sendMessage", {"chat_id": self.chat_id, "text": text,
                                                 "disable_web_page_preview": True})
                self._sent.append(time.monotonic())
            except Exception as e:
                log.warning("telegram send failed: %s", type(e).__name__)
                await asyncio.sleep(2)

    async def _poller(self) -> None:
        offset = 0
        while True:
            try:
                data = await self._call("getUpdates", {"timeout": 25, "offset": offset,
                                                       "allowed_updates": ["message"]}, timeout=35)
                for upd in data.get("result", []):
                    offset = max(offset, int(upd["update_id"]) + 1)
                    msg = upd.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                        continue
                    reply = await self.handle_text(str(msg.get("text") or ""))
                    if reply:
                        self.send(reply)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll failed: %s", type(e).__name__)
                await asyncio.sleep(5)

    async def handle_text(self, text: str) -> str:
        cmd = text.strip().split(" ", 1)[0].lstrip("/").split("@", 1)[0].lower()
        if not cmd:
            return ""
        if cmd in ("help", "start"):
            return HELP
        if cmd == "confirm":
            pending = self._pending_confirm
            self._pending_confirm = None
            if not pending or time.monotonic() - pending[1] > 30:
                return "nothing to confirm"
            cmd = pending[0]
        elif cmd in DANGEROUS:
            self._pending_confirm = (cmd, time.monotonic())
            return f"send /confirm within 30s to {cmd}"
        fn = self.handlers.get(cmd)
        if fn is None:
            return f"unknown command /{cmd}\n{HELP}"
        try:
            out = fn({"cmd": cmd, "source": "telegram"})
            if asyncio.iscoroutine(out):
                out = await out
        except Exception as e:
            return f"/{cmd} failed: {type(e).__name__}: {e}"
        return format_reply(cmd, out)


def format_reply(cmd: str, out: dict) -> str:
    if "text" in out:
        return str(out["text"])
    lines = [f"/{cmd}"]
    for k, v in out.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines.extend(f"  {item}" for item in v[:20])
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)
