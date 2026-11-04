"""Backup bot: a verified archive of your configuration and data every day.

* At ``backup.at`` (IST, every day) it writes ``backups/trading-bots-YYYYMMDD-HHMMSS.tar.gz`` (see
  ``archive.py`` for exactly what goes in and what never does). It reads each archive back against its
  checksums, then prunes old ones: the newest of each of the last ``keep_daily`` days, plus the newest of each
  of the last ``keep_weekly`` weeks.
* With ``backup.rclone_remote`` set (for example ``gdrive:trading-bots``) and the ``rclone`` program installed,
  each new archive is also copied off the machine. Any cloud rclone supports works, with no extra Python
  packages.
* A failure raises an ERROR alert, which the notifier sends to your phone. The latest result is in the
  key-value store under ``backup:status``.
* ``cmd.backup.run`` backs up now; ``cmd.backup.list`` lists the archives. Restoring is done from the command
  line while the team is stopped: ``bots restore <archive>``.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from ...core import topics
from ...core.bot import Bot
from . import archive as AR


class BackupBot(Bot):
    name = "backup"

    def configure(self) -> None:
        self.command("run", self.cmd_run, background=True)
        self.command("list", self.cmd_list)
        self.command("status", self.cmd_status)
        self.daily(lambda: self.settings.backup.at, self.nightly, name="nightly", trading_days_only=False)
        self.last: dict = {}
        self._lock = asyncio.Lock()

    @property
    def out_dir(self) -> Path:
        return self.ctx.paths.resolve(self.settings.backup.dir)

    async def setup(self) -> None:
        found = AR.list_backups(self.out_dir)
        if found:
            self.last = {"saved": True, "file": found[0]["file"], "size": found[0]["size"], "files": found[0]["files"],
                         "time": found[0]["time"], "note": "from an earlier run"}
        await self._publish()

    def details(self) -> dict:
        return {"last": {k: self.last.get(k) for k in ("saved", "file", "time", "error")}}

    def _status(self) -> dict:
        found = AR.list_backups(self.out_dir)
        b = self.settings.backup
        return {"last": self.last, "count": len(found), "total_size": sum(f["size"] for f in found),
                "dir": str(self.out_dir), "enabled": b.enabled, "at": b.at, "ts": time.time()}

    async def _publish(self) -> None:
        await self.bus.kv_set(topics.KV_BACKUP, self._status())

    async def nightly(self) -> None:
        if self.settings.backup.enabled:
            await self.run_backup("daily")

    async def run_backup(self, reason: str) -> dict:
        async with self._lock:
            b = self.settings.backup
            try:
                res = await asyncio.to_thread(AR.create_backup, self.ctx.paths.base / "config", self.ctx.paths.data,
                                              self.out_dir, include_ticks=b.include_ticks,
                                              meta={"reason": reason, "settings_version": self.settings_version})
                res["pruned"] = await asyncio.to_thread(AR.prune, self.out_dir, b.keep_daily, b.keep_weekly)
                if b.rclone_remote:
                    res["upload"] = await self._upload(Path(res["path"]), b.rclone_remote)
                    if not res["upload"]["copied"]:
                        await self.alert(f"backup saved here but not copied to {b.rclone_remote}: "
                                         f"{res['upload']['error']}", "WARNING", key="backup-upload")
                self.last = {"saved": True, "reason": reason, "time": time.strftime("%Y-%m-%d %H:%M:%S"), **res}
                await self.alert(f"backup saved: {res['file']} ({res['files']} files, {res['size'] / 1e6:.1f} MB)",
                                 "INFO", key="backup-done")
            except Exception as e:
                self.last = {"saved": False, "reason": reason, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "error": f"{type(e).__name__}: {e}"}
                self._error("backup", e)
                await self.alert(f"backup FAILED: {type(e).__name__}: {e}", "ERROR", key="backup-failed")
            await self._publish()
            return self.last

    async def _upload(self, path: Path, remote: str) -> dict:
        exe = shutil.which("rclone")
        if not exe:
            return {"copied": False, "error": "rclone is not installed (https://rclone.org/install/)"}
        proc = await asyncio.create_subprocess_exec(exe, "copy", str(path), remote, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            return {"copied": False, "error": "rclone took longer than 15 minutes"}
        if proc.returncode:
            return {"copied": False, "error": err.decode(errors="replace").strip()[-300:] or f"exit {proc.returncode}"}
        return {"copied": True, "remote": remote}

    # ---- commands -------------------------------------------------------------------------------------------
    async def cmd_run(self, data: dict) -> dict:
        r = await self.run_backup("on request")
        return {"done": bool(r.get("saved")), **r}

    async def cmd_list(self, data: dict) -> dict:
        return {"backups": AR.list_backups(self.out_dir)}

    async def cmd_status(self, data: dict) -> dict:
        return self._status()
