"""Backups: what goes in and what never does, consistent database copies, checksums that catch damage,
retention, restore (refused while the team runs; current files moved aside; token kept), and the bot."""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tradingbots.bots.backup import archive as AR
from tradingbots.core import topics

from .helpers import make_settings
from .team import Team


def make_project(base: Path) -> tuple[Path, Path]:
    cfg, data = base / "config", base / "data"
    (cfg).mkdir(parents=True)
    (cfg / "settings.toml").write_text("[platform]\nname = \"simulated\"\n")
    (base / ".env").write_text("DHAN_ACCESS_TOKEN=secret-token\n")
    files = {"tables/trades/date=2026-11-05/part-1.parquet": b"PAR1 fake parquet",
             "bus.db": b"SQLite format 3\x00 not really", "bus.db-wal": b"wal",
             "config/dhan_token.json": b'{"token": "secret-token"}',
             "execution/dhan/token.json": b'{"token": "secret-token"}', "execution/dhan/engine.lock": b"",
             "market_data/instruments/2026-11-05.csv": b"big instrument master",
             "market_data/instruments/2026-11-05.abc.pkl": b"pickle", "ticks/2026-11-05.bin": b"raw ticks",
             "ai_analyst/reports/2026-11-05/r1.json": b'{"headline": "steady"}',
             "tables/money_pnl/part.parquet.tmp": b"half written"}
    for rel, raw in files.items():
        p = data / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    db = sqlite3.connect(data / "journal" / "journal.db") if (data / "journal").mkdir() is None else None
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE notes (k TEXT, v TEXT)")
    db.execute("INSERT INTO notes VALUES ('t1', 'clean entry')")
    db.commit()  # left open, in WAL mode, as a running bot would
    return cfg, data, db


def test_archive_holds_the_right_files_with_checksums(tmp_path):
    cfg, data, db = make_project(tmp_path)
    try:
        r = AR.create_backup(cfg, data, tmp_path / "backups", now=1_793_000_000.0, meta={"reason": "test"})
    finally:
        db.close()
    assert r["verified"] and r["file"].startswith("trading-bots-") and r["file"].endswith(".tar.gz")
    with tarfile.open(r["path"]) as tar:
        names = tar.getnames()
        assert names[0] == "manifest.json"
        restored = tar.extractfile("data/journal/journal.db").read()
    assert set(names) == {"manifest.json", "config/settings.toml", "data/tables/trades/date=2026-11-05/part-1.parquet",
                          "data/ai_analyst/reports/2026-11-05/r1.json", "data/journal/journal.db"}
    body = b"".join(tarfile.open(r["path"]).extractfile(n).read() for n in names if n != "manifest.json")
    assert b"secret-token" not in body  # no token, no .env, no bus
    m = AR.read_manifest(Path(r["path"]))
    assert m["reason"] == "test" and len(m["files"]) == 4 and m["excluded"]["access-token cache"] == 2
    assert "message bus: transient, and it holds the session token" in m["excluded"]
    copy = tmp_path / "copy.db"
    copy.write_bytes(restored)
    assert sqlite3.connect(copy).execute("SELECT v FROM notes").fetchall() == [("clean entry",)]  # WAL data included
    assert (Path(r["path"]).stat().st_mode & 0o777) == 0o600
    with_ticks = AR.create_backup(cfg, data, tmp_path / "backups", include_ticks=True, now=1_793_000_000.0)
    assert with_ticks["file"].endswith("-2.tar.gz") and with_ticks["files"] == 5  # same second: a new name


def test_verify_catches_a_damaged_archive(tmp_path):
    cfg, data, db = make_project(tmp_path)
    db.close()
    r = AR.create_backup(cfg, data, tmp_path / "backups")
    assert AR.verify(Path(r["path"])) == (True, [])
    bad = tmp_path / "trading-bots-20261105-101010.tar.gz"
    with tarfile.open(r["path"]) as src, tarfile.open(bad, "w:gz") as dst:
        for m in src.getmembers():
            raw = src.extractfile(m).read()
            if m.name == "config/settings.toml":
                raw = raw.replace(b"simulated", b"dhan")
                m.size = len(raw)
            dst.addfile(m, io.BytesIO(raw))
    ok, problems = AR.verify(bad)
    assert not ok and problems == ["config/settings.toml: checksum differs"]
    junk = tmp_path / "trading-bots-20261105-111111.tar.gz"
    junk.write_bytes(b"not an archive")
    assert not AR.verify(junk)[0]
    with pytest.raises(AR.BackupError):
        AR.restore(bad, cfg, data, tmp_path / "backups")
    assert (cfg / "settings.toml").read_text().startswith("[platform]")  # nothing changed


def test_retention_keeps_the_last_days_and_weeks(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    start = datetime(2026, 9, 1, 23, 45)
    for i in range(60):  # one a day for 60 days, plus a second one on the last day
        (d / f"trading-bots-{start + timedelta(days=i):%Y%m%d-%H%M%S}.tar.gz").write_bytes(b"x")
    (d / "trading-bots-20261030-120000.tar.gz").write_bytes(b"x")
    (d / "notes.txt").write_text("not a backup: never touched")
    deleted = AR.prune(d, keep_daily=7, keep_weekly=4)
    left = sorted(p.name for p in d.glob("trading-bots-*"))
    days = [f"trading-bots-202610{x:02d}-234500.tar.gz" for x in (11, 18, 24, 25, 26, 27, 28, 29, 30)]
    assert left == days and (d / "notes.txt").exists()  # 7 daily; the 2 newest weeks are among them, +2 older weeks
    assert len(deleted) == 61 - len(left)
    same = tmp_path / "same"  # three in one second: -2 and -3 are newer than the first
    same.mkdir()
    for n in ("", "-2", "-3"):
        (same / f"trading-bots-20261105-101010{n}.tar.gz").write_bytes(b"x")
    AR.prune(same, keep_daily=1, keep_weekly=0)
    assert [p.name for p in same.iterdir()] == ["trading-bots-20261105-101010-3.tar.gz"]


def test_restore_moves_the_current_files_aside_and_keeps_the_token(tmp_path):
    cfg, data, db = make_project(tmp_path)
    db.close()
    r = AR.create_backup(cfg, data, tmp_path / "backups")
    (cfg / "settings.toml").write_text("[platform]\nname = \"dhan\"\n")  # changed after the backup
    (data / "config" / "dhan_token.json").write_text('{"token": "todays-token"}')
    out = AR.restore(Path(r["path"]), cfg, data, tmp_path / "backups")
    assert (cfg / "settings.toml").read_text() == "[platform]\nname = \"simulated\"\n"
    assert (data / "ai_analyst" / "reports" / "2026-11-05" / "r1.json").exists()
    assert json.loads((data / "config" / "dhan_token.json").read_text())["token"] == "todays-token"
    aside = Path(out["moved_aside"])
    assert (aside / "config" / "settings.toml").read_text().endswith("\"dhan\"\n") and (aside / "data" / "bus.db").exists()
    assert out["moved"] == ["config", "data"] and "config/dhan_token.json" in out["tokens_kept"]
    assert (tmp_path / ".env").read_text() == "DHAN_ACCESS_TOKEN=secret-token\n"  # never touched


async def test_backup_bot_runs_on_request_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # no rclone here
    s = make_settings(tmp_path, backup={"rclone_remote": "gdrive:tb", "keep_daily": 2, "keep_weekly": 0})
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.toml").write_text("")
    async with Team(tmp_path, ["backup"], settings=s) as team:
        c = team.client
        r = await c.request(topics.cmd("backup", "run"), {}, timeout=60)
        assert r["done"] and r["verified"] and r["upload"] == {"copied": False,
                                                              "error": "rclone is not installed (https://rclone.org/install/)"}
        st = await c.kv_get(topics.KV_BACKUP)
        assert st["count"] == 1 and st["last"]["file"] == r["file"]
        alerts = [m.data for m in await c.recent(topics.ALERT, 10)]
        assert {a["key"] for a in alerts} >= {"backup-done", "backup-upload"}
        for _ in range(2):
            newest = await c.request(topics.cmd("backup", "run"), {}, timeout=60)
        lst = await c.request(topics.cmd("backup", "list"), {})
        assert [b["file"] for b in lst["backups"]] == [newest["file"]]  # one day: only its newest backup stays
        assert lst["backups"][0]["reason"] == "on request"


def test_cli_restore_refuses_while_the_team_runs(tmp_path, monkeypatch, capsys):
    import asyncio
    import time

    from tradingbots.cli import cmd_restore
    from tradingbots.core.bus import make_bus
    from tradingbots.core.settings import write_settings_file

    cfg, data, db = make_project(tmp_path)
    db.close()
    write_settings_file(cfg / "settings.toml", make_settings(tmp_path))
    r = AR.create_backup(cfg, data, tmp_path / "backups")
    (data / "bus.db").unlink()
    (data / "bus.db-wal").unlink()

    async def beat(status: str) -> None:
        bus = make_bus("sqlite", "money", data_dir=data)
        await bus.connect()
        await bus.kv_set(topics.kv_heartbeat("money"), {"bot": "money", "status": status, "ts": time.time()})
        await bus.close()

    asyncio.run(beat("ok"))
    assert cmd_restore(tmp_path, r["path"], yes=True) == 1
    assert "The team is running (money)" in capsys.readouterr().out
    asyncio.run(beat("stopped"))
    assert cmd_restore(tmp_path, Path(r["path"]).name, yes=True) == 0  # a bare file name is looked up in backups/
    assert "Restored 4 files" in capsys.readouterr().out
