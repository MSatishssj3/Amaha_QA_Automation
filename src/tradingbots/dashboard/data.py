"""How the dashboard reads the team: the key-value store and commands on the bus, the tables with DuckDB.

There is one bus client per dashboard server process (``st.cache_resource``), running on its own
background event loop. Page scripts call it synchronously. The dashboard never trades and never
writes tables; every change goes through a bot's command, so the bot that owns the data validates it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import polars as pl

from ..core import topics
from ..core.bus import BusTimeout, make_bus
from ..core.runtime import Paths
from ..core.settings import Settings, SettingsError, load_settings_file
from ..core.tables_io import TableReader


def base_dir() -> Path:
    """Project folder from ``streamlit run app.py -- --base DIR``, else $TRADINGBOTS_BASE, else the CWD."""
    argv = sys.argv[1:]
    if "--base" in argv:
        i = argv.index("--base")
        if i + 1 < len(argv):
            return Path(argv[i + 1]).resolve()
    return Path(os.environ.get("TRADINGBOTS_BASE") or os.getcwd()).resolve()


class TeamClient:
    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        try:
            self.bootstrap = load_settings_file(self.base / "config" / "settings.toml")
        except SettingsError:
            self.bootstrap = Settings()
        self.paths = Paths(self.base, self.bootstrap)
        self.reader = TableReader(self.paths.tables)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="dashboard-bus", daemon=True).start()
        s = self.bootstrap.system
        self.bus = make_bus(s.bus_backend, "dashboard", data_dir=self.paths.data, redis_url=s.redis_url)
        self.error = ""
        try:
            self._run(self.bus.connect(), 15)
        except Exception as e:  # shown on every page instead of crashing
            self.error = f"cannot open the message bus ({s.bus_backend}): {e}"

    def _run(self, coro, timeout: float = 10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # ---- key-value / events -------------------------------------------------------------------------------
    def kv(self, key: str, default: Any = None) -> Any:
        if self.error:
            return default
        try:
            return self._run(self.bus.kv_get(key, default))
        except Exception:
            return default

    def heartbeats(self) -> dict[str, dict]:
        if self.error:
            return {}
        try:
            keys = self._run(self.bus.kv_keys("hb:"))
        except Exception:
            return {}
        out = {}
        for k in keys:
            hb = self.kv(k)
            if hb:
                out[k[3:]] = hb
        return out

    def recent(self, topic: str, limit: int = 100) -> list[dict]:
        """Newest first: [{ts, source, **data}]."""
        if self.error:
            return []
        try:
            msgs = self._run(self.bus.recent(topic, limit))
        except Exception:
            return []
        return [{"ts": m.ts, "source": m.source, **m.data} for m in reversed(msgs)]

    def command(self, bot: str, action: str, data: dict | None = None, timeout: float = 8.0) -> dict:
        """Ask a bot; a bot that is down answers with ok=False and an explanation, never an exception."""
        if self.error:
            return {"ok": False, "error": self.error}
        try:
            return self._run(self.bus.request(topics.cmd(bot, action), dict(data or {}), timeout), timeout + 5)
        except (BusTimeout, TimeoutError, asyncio.TimeoutError):
            return {"ok": False, "error": f"the {bot} bot did not answer within {timeout:.0f}s - is it running?"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- settings ---------------------------------------------------------------------------------------------
    def settings(self) -> tuple[Settings, int]:
        cur = self.kv(topics.KV_CONFIG)
        if cur:
            try:
                from ..core.settings import parse_settings

                return parse_settings(cur["settings"]), int(cur.get("version", 0))
            except Exception:
                pass
        return self.bootstrap, 0

    # ---- tables -------------------------------------------------------------------------------------------------
    def query(self, sql: str, params: list | None = None) -> pl.DataFrame:
        try:
            return self.reader.query(sql, params)
        except Exception:
            return pl.DataFrame()

    def table(self, name: str, where: str = "", params: list | None = None, order: str = "",
              limit: int | None = None) -> pl.DataFrame:
        try:
            return self.reader.table(name, where, params, order, limit)
        except Exception:
            return pl.DataFrame()

    def available(self) -> list[str]:
        try:
            return self.reader.available()
        except Exception:
            return []


def client() -> TeamClient:
    import streamlit as st

    @st.cache_resource(show_spinner=False)
    def _make(base: str) -> TeamClient:
        return TeamClient(Path(base))

    return _make(str(base_dir()))


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "-"
    s = max(0.0, (now or time.time()) - float(ts))
    if s < 90:
        return f"{s:.0f}s ago"
    if s < 5400:
        return f"{s / 60:.0f} min ago"
    if s < 172800:
        return f"{s / 3600:.0f} h ago"
    return f"{s / 86400:.0f} days ago"
