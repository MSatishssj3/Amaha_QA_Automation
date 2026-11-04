"""Raw market-feed recording for replay / backtesting.

File format (``data/ticks/YYYY-MM-DD.bin``): a sequence of records ``[u64 wall_ns][u32 len][len bytes]``
where the bytes are the exact WebSocket frame received from Dhan. Replaying feeds those frames
through the same parser + engine code path used live.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

_HDR = struct.Struct("<QI")


class TickRecorder:
    def __init__(self, path: Path, flush_s: float = 1.0) -> None:
        self.path = path
        self.flush_s = flush_s
        self._buf = bytearray()
        self._task: asyncio.Task | None = None
        self.frames = 0
        self.bytes = 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, frame: bytes, wall_ns: int | None = None) -> None:
        self._buf += _HDR.pack(wall_ns or time.time_ns(), len(frame))
        self._buf += frame
        self.frames += 1

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_s)
            await self.flush()

    def _append(self, data: bytes) -> None:
        with self.path.open("ab") as fh:
            fh.write(data)

    async def flush(self) -> None:
        if not self._buf:
            return
        data, self._buf = bytes(self._buf), bytearray()
        self.bytes += len(data)
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._append, data)
        except Exception:
            log.exception("tick recorder write failed")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self.flush()


def write_frames(path: Path, frames: list[tuple[int, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        for ns, frame in frames:
            fh.write(_HDR.pack(ns, len(frame)))
            fh.write(frame)


def iter_frames(path: Path) -> Iterator[tuple[int, bytes]]:
    with path.open("rb") as fh:
        while True:
            hdr = fh.read(_HDR.size)
            if len(hdr) < _HDR.size:
                return
            ns, n = _HDR.unpack(hdr)
            frame = fh.read(n)
            if len(frame) < n:
                log.warning("%s: truncated last record ignored", path)
                return
            yield ns, frame
