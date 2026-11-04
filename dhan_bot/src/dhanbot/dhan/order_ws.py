"""Order-update WebSocket (``wss://api-order-update.dhan.co``).

Pushes order state changes (acks, fills, rejects, cancels) as JSON ``order_alert`` messages - far
faster than polling the order book. While this socket is down, the reconciler polls ``GET /orders``
every second instead (see ``reconcile.py``).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable

import aiohttp
import orjson

from ..models import BrokerUpdate
from .feed import BACKOFF
from .normalize import normalise_order

log = logging.getLogger(__name__)


class OrderUpdateConnection:
    def __init__(self, url: str, client_id: str, token_provider: Callable[[], str],
                 on_update: Callable[[BrokerUpdate], None], *, on_event: Callable[[str, dict], None] | None = None,
                 on_raw: Callable[[dict], None] | None = None, heartbeat_s: float = 10.0) -> None:
        self.url = url
        self.client_id = client_id
        self.token_provider = token_provider
        self.on_update = on_update
        self.on_event = on_event or (lambda kind, info: None)
        self.on_raw = on_raw
        self.heartbeat_s = heartbeat_s
        self.connected = False
        self.messages = 0
        self._stop = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._first_logged = False

    def login_message(self) -> str:
        return orjson.dumps({"LoginReq": {"MsgCode": 42, "ClientId": str(self.client_id),
                                          "Token": str(self.token_provider())}, "UserType": "SELF"}).decode()

    async def run(self) -> None:
        attempt = 0
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                got = False
                try:
                    async with session.ws_connect(self.url, heartbeat=self.heartbeat_s, autoping=True) as ws:
                        self._ws = ws
                        await ws.send_str(self.login_message())
                        self.connected = True
                        self.on_event("connected", {})
                        async for msg in ws:
                            if msg.type is aiohttp.WSMsgType.TEXT:
                                got = True
                                self._handle(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    log.warning("order-update socket error: %s", type(e).__name__ + (f": {e}" if str(e) else ""))
                finally:
                    was = self.connected
                    self.connected = False
                    self._ws = None
                    if was:
                        self.on_event("disconnected", {})
                if self._stop:
                    break
                attempt = 0 if got else attempt + 1
                await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)] * random.uniform(0.8, 1.2))

    def _handle(self, text: str) -> None:
        try:
            msg = orjson.loads(text)
        except orjson.JSONDecodeError:
            log.warning("order-update: non-JSON message %r", text[:200])
            return
        if not isinstance(msg, dict):
            return
        if msg.get("Type") != "order_alert":
            log.info("order-update: %s", text[:300])
            return
        data = msg.get("Data") or {}
        if not self._first_logged:  # field names could not be verified offline: keep one raw sample
            self._first_logged = True
            log.info("order-update first raw message: %s", text[:2000])
            if self.on_raw:
                self.on_raw(data)
        upd = normalise_order(data) if isinstance(data, dict) else None
        if upd is None:
            log.warning("order-update without order number: %s", text[:300])
            return
        self.messages += 1
        try:
            self.on_update(upd)
        except Exception:
            log.exception("order-update handler raised")

    async def recycle(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
