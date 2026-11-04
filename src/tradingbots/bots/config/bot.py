"""Config bot: owns the settings (versioned, validated, audited) and the trading-platform credentials.

* ``cmd.config.get`` / ``update`` (a partial patch, e.g. ``{"platform": {"name": "dhan"}}``) /
  ``history`` / ``rollback`` / ``set_token`` / ``token_status``.
* Broadcasts ``config.changed`` and keeps ``config:current`` in the key-value store.
* **The only process that logs in to Dhan** (token pasted on the dashboard, or automatic PIN+TOTP
  login) - other bots receive the token through ``secret:token`` + ``auth.token``. A single owner
  stops processes from invalidating each other's tokens.
"""

from __future__ import annotations

import time
from typing import Any

from dhanbot.clock import epoch_at
from dhanbot.config import load_config as load_dhan_config
from dhanbot.dhan.auth import AuthError, TokenManager
from dhanbot.dhan.endpoints import Endpoints

from ...core import topics
from ...core.bot import Bot
from ...core.secrets import load_env
from ...core.settings import Settings, SettingsError, deep_merge, parse_settings
from .store import SettingsStore

RESTART_KEYS = ("system.",)


class ConfigBot(Bot):
    name = "config"

    def configure(self) -> None:
        self.command("get", self.cmd_get)
        self.command("update", self.cmd_update)
        self.command("history", self.cmd_history)
        self.command("rollback", self.cmd_rollback)
        self.command("set_token", self.cmd_set_token)
        self.command("token_status", self.cmd_token_status)
        self.every(300.0, self.refresh_token, name="token-check")
        self.every(5.0, self.watch_sim, name="sim-watch")
        self.token_version = 0
        self.token_info: dict[str, Any] = {}
        self.token_error = ""
        self._tm: TokenManager | None = None

    async def setup(self) -> None:
        self.store = SettingsStore(self.ctx.paths.bot("config") / "settings.db", self.ctx.paths.settings_file)
        try:
            version, settings, changes = self.store.load_or_import(self.ctx.bootstrap)
        except SettingsError as e:
            await self.alert(f"config/settings.toml is invalid, keeping the last good version: {e}", "ERROR")
            cur = self.store.current()
            version, settings, changes = (cur[0], cur[1], []) if cur else (0, Settings(), [])
        await self._publish(version, settings, changes, "startup")
        await self.refresh_token()

    async def teardown(self) -> None:
        self.store.close()

    def details(self) -> dict:
        return {"version": self.settings_version, "platform": self.settings.platform.name,
                "token_expires_at": self.token_info.get("expires_at"), "token_error": self.token_error}

    async def _publish(self, version: int, settings: Settings, changes: list[str], author: str) -> None:
        self.settings, self.settings_version = settings, version
        await self.bus.kv_set(topics.KV_CONFIG, {"version": version, "settings": settings.to_dict()})
        await self.publish(topics.CONFIG_CHANGED, {"version": version, "changed": changes, "author": author})

    # ---- commands ----------------------------------------------------------------------------
    async def cmd_get(self, data: dict) -> dict:
        return {"version": self.settings_version, "settings": self.settings.to_dict()}

    async def cmd_update(self, data: dict) -> dict:
        patch = data.get("patch") or {}
        author = str(data.get("author") or "dashboard")
        try:
            new = parse_settings(deep_merge(self.settings.to_dict(), patch))
        except SettingsError as e:
            return {"applied": False, "errors": e.problems}
        old = self.settings
        version, changes = self.store.save(new, author, str(data.get("note") or ""))
        if not changes:
            return {"applied": False, "version": version, "changes": [], "errors": []}
        await self._publish(version, new, changes, author)
        if new.platform != old.platform:
            self._tm = None  # auth mode may have changed
            await self.refresh_token(force=True)
        restart = [c for c in changes if c.startswith(RESTART_KEYS)]
        return {"applied": True, "version": version, "changes": changes, "restart_required": restart, "errors": []}

    async def cmd_history(self, data: dict) -> dict:
        return {"versions": self.store.history(int(data.get("limit") or 50))}

    async def cmd_rollback(self, data: dict) -> dict:
        target = self.store.get(int(data["version"]))
        if target is None:
            return {"applied": False, "errors": [f"no version {data['version']}"]}
        old = self.settings
        version, changes = self.store.save(target, str(data.get("author") or "dashboard"),
                                           f"rollback to v{data['version']}")
        if changes:
            await self._publish(version, target, changes, "rollback")
            if target.platform != old.platform:
                self._tm = None
                await self.refresh_token(force=True)
        return {"applied": bool(changes), "version": version, "changes": changes}

    async def cmd_set_token(self, data: dict) -> dict:
        token = str(data.get("token") or "").strip()
        if not token:
            return {"saved": False, "error": "empty token"}
        tm = self._token_manager()
        tm.save_manual(token)
        await self._share_token("dhan", tm.client_id, tm.token, tm.expires_at, None)
        return {"saved": True, "expires_at": tm.expires_at}

    async def cmd_token_status(self, data: dict) -> dict:
        info = {k: v for k, v in self.token_info.items() if k != "token"}
        return {"platform": self.settings.platform.name, "info": info, "error": self.token_error,
                "seconds_left": (info.get("expires_at") or 0) - self.clock.time() if info.get("expires_at") else None}

    # ---- credentials ------------------------------------------------------------------------------
    def _token_manager(self) -> TokenManager:
        if self._tm is None:
            env = load_env(self.ctx.paths.base)
            cfg = load_dhan_config(data={"dhan": {"auth": self.settings.platform.dhan_auth},
                                         "paths": {"data_dir": str(self.ctx.paths.bot("config"))}}, env=env)
            self._tm = TokenManager(cfg, Endpoints(), self.clock, cache_path=self.ctx.paths.bot("config") / "dhan_token.json")
        return self._tm

    async def _share_token(self, platform: str, client_id: str, token: str, expires_at: float | None,
                           endpoints: dict | None) -> None:
        changed = token != self.token_info.get("token") or platform != self.token_info.get("platform")
        self.token_error = ""
        if not changed:
            return
        self.token_version += 1
        self.token_info = {"platform": platform, "client_id": client_id, "token": token, "expires_at": expires_at,
                           "version": self.token_version, "endpoints": endpoints, "updated": time.time()}
        await self.bus.kv_set(topics.KV_TOKEN, self.token_info)
        await self.publish(topics.AUTH_TOKEN, {"platform": platform, "version": self.token_version,
                                               "expires_at": expires_at})
        self.log.info("access token for %s shared (v%d)", platform, self.token_version)

    async def refresh_token(self, force: bool = False) -> None:
        platform = self.settings.platform.name
        if platform == "simulated":
            await self.watch_sim()
            return
        tm = self._token_manager()
        today = self.ctx.today()
        valid_until = epoch_at(today, self.settings.platform.token_valid_until)
        try:
            await tm.ensure(valid_until=max(valid_until, self.clock.time() + 3600),
                            allow_login=self.settings.platform.dhan_auth == "totp")
        except AuthError as e:
            self.token_error = str(e)
            await self.alert(f"no valid Dhan access token: {e}", "ERROR", key="token")
            return
        await self._share_token("dhan", tm.client_id, tm.token, tm.expires_at, None)

    async def watch_sim(self) -> None:
        if self.settings.platform.name != "simulated":
            return
        info = await self.bus.kv_get(topics.KV_SIM)
        if not info:
            self.token_error = "simulated market not running (start the sim_market bot)"
            return
        await self._share_token("simulated", info["client_id"], info["token"], None, info["endpoints"])
