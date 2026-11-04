from __future__ import annotations

import asyncio
import time
from typing import Callable


async def wait_for(cond: Callable[[], bool], timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return cond()
