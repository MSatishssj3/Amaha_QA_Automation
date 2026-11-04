"""Local control channel for a running bot (``dhanbot status / pause / resume / flatten / killswitch``).

A tiny JSON-lines server on 127.0.0.1 with a random port + secret token written to
``data/control.json`` (chmod 600). Works the same on Linux and Windows. Only processes that can read
that file (i.e. your user) can control the bot.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Awaitable, Callable

import orjson

from .dhan.auth import write_private

log = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[dict] | dict]


class ControlServer:
    def __init__(self, path: Path, handlers: dict[str, Handler]) -> None:
        self.path = path
        self.handlers = handlers
        self.token = secrets.token_hex(16)
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        write_private(self.path, orjson.dumps({"port": self.port, "token": self.token}))
        log.info("control channel on 127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            self.path.unlink()
        except OSError:
            pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            req = orjson.loads(line or b"{}")
            if not secrets.compare_digest(str(req.get("token", "")), self.token):
                resp = {"ok": False, "error": "bad token"}
            else:
                fn = self.handlers.get(str(req.get("cmd")))
                if fn is None:
                    resp = {"ok": False, "error": f"unknown command; try one of {sorted(self.handlers)}"}
                else:
                    out = fn(req)
                    if asyncio.iscoroutine(out):
                        out = await out
                    resp = {"ok": True, **out}
        except Exception as e:  # never let a control client crash the bot
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            writer.write(orjson.dumps(resp, default=str) + b"\n")
            await writer.drain()
        finally:
            writer.close()


async def send_command(path: Path, cmd: str, timeout: float = 30.0, **args) -> dict:
    if not path.exists():
        return {"ok": False, "error": f"no running bot found ({path} missing)"}
    info = orjson.loads(path.read_bytes())
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", info["port"]), 5)
    except OSError as e:
        return {"ok": False, "error": f"bot not reachable: {e}"}
    writer.write(orjson.dumps({"cmd": cmd, "token": info["token"], **args}) + b"\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout)
    writer.close()
    return orjson.loads(line or b"{}")
