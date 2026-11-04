"""Backup archives: what goes in, how the copies stay consistent, and how archives are verified, pruned and
restored.

* **In:** ``config/`` and the data folder: tables, every bot's database, reports, the engine's state.
* **Never in:**
  - ``.env`` (it sits outside both folders);
  - the message bus (``bus.db*``): it is transient and holds the session token;
  - access-token caches (``*token.json``);
  - lock and temp files;
  - instrument caches, which are downloaded again;
  - raw tick recordings, unless ``backup.include_ticks``.
* **Consistent copies.** SQLite databases are copied with SQLite's online backup, so a database in use is still
  copied consistently. Parquet files never change once written.
* **Layout.** ``trading-bots-YYYYMMDD-HHMMSS.tar.gz`` (IST) holds ``manifest.json`` first: every file's size and
  sha256, plus what was left out and why. Then come ``config/...`` and ``data/...``. Each archive is read back
  and checked against its manifest before it is kept.
* **Restore.** It moves the current ``config/`` and data folder aside, never deleting them, then unpacks the
  archive. Your current access-token caches are carried over.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import tarfile
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from dhanbot.clock import ist_datetime

from ... import __version__

PREFIX = "trading-bots-"
NAME_RE = re.compile(r"^trading-bots-(\d{8})-(\d{6})(?:-(\d+))?\.tar\.gz$")
SQLITE_MAGIC = b"SQLite format 3\x00"


class BackupError(Exception):
    pass


def excluded(rel: str, include_ticks: bool = False) -> str:
    """Why the logical path ``rel`` (``config/...`` or ``data/...``) stays out of backups; "" when it goes in."""
    name = rel.rsplit("/", 1)[-1]
    if name == ".env" or name.endswith(".env"):
        return "secrets (.env)"
    if rel.startswith("data/bus.db"):
        return "message bus: transient, and it holds the session token"
    if name.endswith("token.json"):
        return "access-token cache"
    if name.endswith(("-wal", "-shm", "-journal")):
        return "database journal (the database itself is copied consistently)"
    if name.endswith((".lock", ".tmp", ".partial", ".pkl")):
        return "lock, temporary or cache file"
    if "/instruments/" in rel:
        return "instrument cache (downloaded again)"
    if rel.startswith("data/ticks/") and not include_ticks:
        return "raw tick recordings (backup.include_ticks = false)"
    return ""


def _is_sqlite(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _sqlite_copy(src: Path, dst: Path) -> None:
    s = sqlite3.connect(str(src), timeout=15)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def _sha256(fobj) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: fobj.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def archive_time(name: str) -> datetime | None:
    m = NAME_RE.match(name)
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None


def _order(name: str) -> tuple[str, str, int]:
    """Oldest to newest, including several archives made in the same second (-2, -3 ...)."""
    m = NAME_RE.match(name)
    return (m.group(1), m.group(2), int(m.group(3) or 1)) if m else ("", "", 0)


def create_backup(config_dir: Path, data_dir: Path, out_dir: Path, *, include_ticks: bool = False,
                  now: float | None = None, meta: dict | None = None) -> dict:
    """Write one verified archive into ``out_dir`` and return what it holds."""
    now = time.time() if now is None else now
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{ist_datetime(now):%Y%m%d-%H%M%S}"
    same = [_order(p.name)[2] for p in out_dir.glob(f"{PREFIX}{stamp}*.tar.gz") if NAME_RE.match(p.name)]
    # several in one second count up (-2, -3 ...), never reusing a name pruning has freed
    final = out_dir / (f"{PREFIX}{stamp}-{max(same) + 1}.tar.gz" if same else f"{PREFIX}{stamp}.tar.gz")
    partial = final.with_name(final.name + ".partial")
    stage = out_dir / f".staging-{stamp}-{os.getpid()}"
    out_real = out_dir.resolve()
    files: list[dict] = []
    skipped: Counter = Counter()
    problems: list[str] = []
    try:
        for logical, root in (("config", Path(config_dir)), ("data", Path(data_dir))):
            if not root.exists():
                continue
            for p in sorted(root.rglob("*")):
                if p.is_symlink() or not p.is_file() or out_real in p.resolve().parents:
                    continue
                rel = f"{logical}/{p.relative_to(root).as_posix()}"
                why = excluded(rel, include_ticks)
                if why:
                    skipped[why] += 1
                    continue
                dst = stage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if _is_sqlite(p):
                        _sqlite_copy(p, dst)
                    else:
                        shutil.copy2(p, dst)
                except FileNotFoundError:  # replaced while copying (table compaction): its successor is newer anyway
                    continue
                except (OSError, sqlite3.Error) as e:
                    problems.append(f"{rel}: {type(e).__name__}: {e}")
                    continue
                with dst.open("rb") as fh:
                    files.append({"path": rel, "size": dst.stat().st_size, "sha256": _sha256(fh)})
        manifest = {"app": "trading-bots", "version": __version__, "created": f"{ist_datetime(now):%Y-%m-%d %H:%M:%S} IST",
                    "created_ts": now, "host": socket.gethostname(), "files": files, "excluded": dict(skipped),
                    "problems": problems, **(meta or {})}
        with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
            raw = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime, info.mode = len(raw), int(now), 0o600
            tar.addfile(info, io.BytesIO(raw))
            for f in files:
                tar.add(stage / f["path"], arcname=f["path"], recursive=False)
        ok, issues = verify(partial)
        if not ok:
            raise BackupError("the new archive failed its own check: " + "; ".join(issues[:3]))
        os.replace(partial, final)
        try:
            os.chmod(final, 0o600)  # it holds your trading records
        except OSError:
            pass
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        partial.unlink(missing_ok=True)
    return {"file": final.name, "path": str(final), "size": final.stat().st_size, "files": len(files),
            "excluded": dict(skipped), "problems": problems, "verified": True, "created_ts": now}


def read_manifest(path: Path) -> dict:
    with tarfile.open(path, "r:gz") as tar:
        first = tar.next()
        if first is None or first.name != "manifest.json":
            raise BackupError(f"{Path(path).name}: no manifest.json at the start - not a trading-bots backup")
        fh = tar.extractfile(first)
        assert fh is not None
        return json.loads(fh.read())


def verify(path: Path) -> tuple[bool, list[str]]:
    """Read every file back and compare it with the manifest."""
    problems: list[str] = []
    try:
        manifest = read_manifest(path)
        want = {f["path"]: f for f in manifest["files"]}
        seen: set[str] = set()
        with tarfile.open(path, "r:gz") as tar:
            for m in tar:
                if m.name == "manifest.json":
                    continue
                if not m.isfile():
                    problems.append(f"{m.name}: not a regular file")
                elif m.name not in want:
                    problems.append(f"{m.name}: not in the manifest")
                else:
                    fh = tar.extractfile(m)
                    assert fh is not None
                    if _sha256(fh) != want[m.name]["sha256"]:
                        problems.append(f"{m.name}: checksum differs")
                    seen.add(m.name)
        problems += [f"{p}: missing" for p in sorted(set(want) - seen)]
    except (tarfile.TarError, OSError, KeyError, ValueError, BackupError, EOFError) as e:
        problems.append(f"cannot read the archive: {e}")
    return not problems, problems


def list_backups(out_dir: Path) -> list[dict]:
    """Archives in ``out_dir``, newest first (time from the file name; details from each manifest)."""
    out = []
    d = Path(out_dir)
    if not d.exists():
        return out
    for p in d.iterdir():
        t = archive_time(p.name)
        if t is None or not p.is_file():
            continue
        row = {"file": p.name, "path": str(p), "size": p.stat().st_size, "time": t.strftime("%Y-%m-%d %H:%M:%S"),
               "files": None, "reason": "", "version": ""}
        try:
            m = read_manifest(p)
            row.update(files=len(m.get("files") or []), reason=m.get("reason", ""), version=m.get("version", ""))
        except Exception as e:
            row["reason"] = f"unreadable: {e}"
        out.append(row)
    return sorted(out, key=lambda r: _order(r["file"]), reverse=True)


def prune(out_dir: Path, keep_daily: int, keep_weekly: int) -> list[str]:
    """Keep the newest archive of each of the last ``keep_daily`` days, plus the newest of each of the last
    ``keep_weekly`` ISO weeks; delete the other archives (only files named like ours)."""
    arch = sorted(((archive_time(p.name), p) for p in Path(out_dir).glob(f"{PREFIX}*.tar.gz") if archive_time(p.name)),
                  key=lambda tp: _order(tp[1].name), reverse=True)
    keep: set[Path] = set()
    days: list[date] = []
    weeks: list[tuple[int, int]] = []
    for t, p in arch:
        if t.date() not in days and len(days) < keep_daily:
            days.append(t.date())
            keep.add(p)
    for t, p in arch:
        wk = tuple(t.isocalendar())[:2]
        if wk not in weeks and len(weeks) < keep_weekly:
            weeks.append(wk)  # type: ignore[arg-type]
            keep.add(p)
    deleted = []
    for _, p in arch:
        if p not in keep:
            p.unlink(missing_ok=True)
            deleted.append(p.name)
    return deleted


def restore(archive: Path, config_dir: Path, data_dir: Path, aside_root: Path, now: float | None = None) -> dict:
    """Move the current folders aside, then unpack ``archive``. The caller makes sure no bot is running."""
    archive = Path(archive)
    ok, problems = verify(archive)
    if not ok:
        raise BackupError("the archive failed its check, nothing was changed: " + "; ".join(problems[:5]))
    now = time.time() if now is None else now
    stamp = f"{ist_datetime(now):%Y%m%d-%H%M%S}"
    aside = Path(aside_root) / f"before-restore-{stamp}"
    work = Path(aside_root) / f".restore-{stamp}"
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name != "manifest.json"]
        for m in members:
            parts = m.name.split("/")
            if parts[0] not in ("config", "data") or ".." in parts or m.name.startswith("/") or not m.isfile():
                raise BackupError(f"refusing an unexpected entry in the archive: {m.name}")
        work.mkdir(parents=True)
        tar.extractall(work, members=members, filter="data")
    aside.mkdir(parents=True)
    moved = []
    for logical, d in (("config", Path(config_dir)), ("data", Path(data_dir))):
        if d.exists():
            shutil.move(str(d), str(aside / logical))
            moved.append(logical)
        src = work / logical
        if src.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(d))
        else:
            d.mkdir(parents=True, exist_ok=True)
    kept = []
    old_data = aside / "data"
    if old_data.exists():  # your current access tokens: a backup never holds them, so carry them over
        for tok in old_data.rglob("*token.json"):
            target = Path(data_dir) / tok.relative_to(old_data)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tok, target)
            kept.append(tok.relative_to(old_data).as_posix())
    shutil.rmtree(work, ignore_errors=True)
    return {"restored": archive.name, "files": len(members), "moved_aside": str(aside), "moved": moved,
            "tokens_kept": kept}
