"""Market-feed WebSocket connection (binary DhanHQ v2 live feed).

* The frame callback runs **synchronously inside the receive loop** - no queue hop between the
  socket and the strategy. Keep the callback fast; it must not await.
* Subscriptions are tracked as a desired set, so every reconnect resubscribes automatically
  (batched <= 100 instruments per message, highest requested mode per instrument).
* Reconnect with exponential backoff + jitter. Disconnect codes 806/808/809 are fatal (no retry);
  807 (token expired) raises a ``token_expired`` event so the engine can refresh the token first.
* Watchdog: if no frame arrives for ``2 x stale_after_s`` while data is expected, the socket is
  recycled.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Callable, Iterable

import aiohttp
import orjson

from .endpoints import DISCONNECT_REASONS, FATAL_DISCONNECTS, MODE_RANK, REQ_DISCONNECT
from .packets import _DISC, CODE_DISCONNECT

log = logging.getLogger(__name__)

BACKOFF = (0.25, 0.5, 1.0, 2.0, 5.0)
BATCH = 100


class FeedConnection:
    def __init__(self, name: str, url_factory: Callable[[], str], on_frame: Callable[[bytes], None], *,
                 on_event: Callable[[str, dict], None] | None = None, heartbeat_s: float = 10.0,
                 stale_after_s: float = 5.0, expect_data: Callable[[], bool] | None = None) -> None:
        self.name = name
        self.url_factory = url_factory
        self.on_frame = on_frame
        self.on_event = on_event or (lambda kind, info: None)
        self.heartbeat_s = heartbeat_s
        self.stale_after_s = stale_after_s
        self.expect_data = expect_data or (lambda: True)
        self.desired: dict[tuple[str, int], int] = {}  # (segment, security_id) -> request code
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._stop = False
        self.fatal_reason: int | None = None
        self.connected = False
        self.last_frame_mono = 0.0
        self.frames = 0
        self.reconnects = 0
        self._recent_failures: list[float] = []
        self._frame_errors = 0

    # ---- subscriptions -------------------------------------------------------------------
    def subscribe(self, items: Iterable[tuple[str, int]], mode: int) -> None:
        added: list[tuple[str, int]] = []
        for key in items:
            key = (key[0], int(key[1]))
            cur = self.desired.get(key)
            if cur is None or MODE_RANK[mode] > MODE_RANK[cur]:
                self.desired[key] = mode
                added.append(key)
        if added and self._ws is not None and not self._ws.closed:
            asyncio.ensure_future(self._send_subs(self._ws, {k: self.desired[k] for k in added}))

    def unsubscribe(self, items: Iterable[tuple[str, int]]) -> None:
        removed: dict[tuple[str, int], int] = {}
        for key in items:
            key = (key[0], int(key[1]))
            mode = self.desired.pop(key, None)
            if mode is not None:
                removed[key] = mode
        if removed and self._ws is not None and not self._ws.closed:
            asyncio.ensure_future(self._send_subs(self._ws, removed, unsubscribe=True))

    @staticmethod
    def build_messages(subs: dict[tuple[str, int], int], unsubscribe: bool = False) -> list[bytes]:
        by_mode: dict[int, list[tuple[str, int]]] = {}
        for key, mode in subs.items():
            by_mode.setdefault(mode, []).append(key)
        out: list[bytes] = []
        for mode, keys in sorted(by_mode.items()):
            for i in range(0, len(keys), BATCH):
                batch = keys[i:i + BATCH]
                out.append(orjson.dumps({
                    "RequestCode": mode + 1 if unsubscribe else mode,
                    "InstrumentCount": len(batch),
                    "InstrumentList": [{"ExchangeSegment": seg, "SecurityId": str(sid)} for seg, sid in batch],
                }))
        return out

    async def _send_subs(self, ws: aiohttp.ClientWebSocketResponse, subs: dict[tuple[str, int], int],
                         unsubscribe: bool = False) -> None:
        try:
            for msg in self.build_messages(subs, unsubscribe):
                await ws.send_str(msg.decode())
        except Exception as e:
            log.warning("[%s] subscription send failed: %s", self.name, e)

    # ---- run loop ------------------------------------------------------------------------
    async def run(self) -> None:
        self._session = aiohttp.ClientSession()
        watchdog = asyncio.create_task(self._watchdog())
        attempt = 0
        try:
            while not self._stop:
                got_data = False
                try:
                    async with self._session.ws_connect(self.url_factory(), heartbeat=self.heartbeat_s,
                                                        compress=0, max_msg_size=0, autoping=True) as ws:
                        self._ws = ws
                        self.connected = True
                        self.last_frame_mono = time.monotonic()
                        await self._send_subs(ws, dict(self.desired))
                        self.on_event("connected", {"name": self.name, "instruments": len(self.desired)})
                        async for msg in ws:
                            if msg.type is aiohttp.WSMsgType.BINARY:
                                data = msg.data
                                self.last_frame_mono = time.monotonic()
                                self.frames += 1
                                got_data = True
                                if data[0] == CODE_DISCONNECT and len(data) >= 10:
                                    self._handle_disconnect(_DISC.unpack_from(data, 0)[4])
                                    break
                                try:
                                    self.on_frame(data)
                                except Exception:
                                    self._frame_errors += 1
                                    if self._frame_errors <= 5 or self._frame_errors % 1000 == 0:
                                        log.exception("[%s] frame handler raised (count=%d)", self.name, self._frame_errors)
                            elif msg.type is aiohttp.WSMsgType.TEXT:
                                log.info("[%s] text message: %s", self.name, msg.data[:200])
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    log.warning("[%s] connection error: %s", self.name, type(e).__name__ + (f": {e}" if str(e) else ""))
                finally:
                    was = self.connected
                    self.connected = False
                    self._ws = None
                    if was:
                        self.on_event("disconnected", {"name": self.name})
                if self._stop or self.fatal_reason is not None:
                    break
                attempt = 0 if got_data else attempt + 1
                self._note_failure()
                self.reconnects += 1
                delay = BACKOFF[min(attempt, len(BACKOFF) - 1)] * random.uniform(0.8, 1.2)
                await asyncio.sleep(delay)
        finally:
            watchdog.cancel()
            await self._session.close()
            self._session = None

    def _handle_disconnect(self, reason: int) -> None:
        text = DISCONNECT_REASONS.get(reason, "unknown")
        if reason in FATAL_DISCONNECTS:
            self.fatal_reason = reason
            log.error("[%s] server disconnected us: %d (%s) - not retrying", self.name, reason, text)
            self.on_event("fatal", {"name": self.name, "reason": reason, "text": text})
        elif reason == 807:
            log.warning("[%s] feed says access token expired (807)", self.name)
            self.on_event("token_expired", {"name": self.name})
        else:
            log.warning("[%s] server disconnect %d (%s)", self.name, reason, text)

    def _note_failure(self) -> None:
        now = time.monotonic()
        self._recent_failures = [t for t in self._recent_failures if now - t < 60] + [now]
        if len(self._recent_failures) == 3:
            self.on_event("unstable", {"name": self.name, "failures_60s": 3})

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            ws = self._ws
            if ws is None or ws.closed or not self.desired or not self.expect_data():
                continue
            silent = time.monotonic() - self.last_frame_mono
            if silent > 2 * self.stale_after_s:
                log.warning("[%s] no data for %.1fs - recycling connection", self.name, silent)
                self.on_event("stale", {"name": self.name, "silent_s": silent})
                self.last_frame_mono = time.monotonic()
                await ws.close()

    async def recycle(self) -> None:
        """Force a reconnect (e.g. after the token was refreshed)."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def stop(self) -> None:
        self._stop = True
        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await ws.send_str(orjson.dumps({"RequestCode": REQ_DISCONNECT}).decode())
            except Exception:
                pass
            await ws.close()

    @property
    def silent_for(self) -> float:
        return time.monotonic() - self.last_frame_mono if self.last_frame_mono else float("inf")
