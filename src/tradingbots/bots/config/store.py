"""Versioned settings store (SQLite): every change is validated, diffed, attributed and kept, so any
version can be inspected or rolled back. ``config/settings.toml`` mirrors the current version; if you
edit that file, the change is imported as a new version on the next start."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import orjson

from ...core.settings import Settings, diff, load_settings_file, parse_settings, write_settings_file


class SettingsStore:
    def __init__(self, db_path: Path, toml_path: Path) -> None:
        self.toml_path = toml_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS versions (version INTEGER PRIMARY KEY, ts REAL, author TEXT, "
                        "note TEXT, diff TEXT, settings TEXT, file_hash TEXT)")

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""

    def current(self) -> tuple[int, Settings] | None:
        row = self.db.execute("SELECT version, settings FROM versions ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return int(row[0]), parse_settings(orjson.loads(row[1]))

    def get(self, version: int) -> Settings | None:
        row = self.db.execute("SELECT settings FROM versions WHERE version=?", (version,)).fetchone()
        return parse_settings(orjson.loads(row[0])) if row else None

    def last_file_hash(self) -> str:
        row = self.db.execute("SELECT file_hash FROM versions ORDER BY version DESC LIMIT 1").fetchone()
        return row[0] if row else ""

    def save(self, new: Settings, author: str, note: str = "") -> tuple[int, list[str]]:
        cur = self.current()
        changes = diff(cur[1].to_dict(), new.to_dict()) if cur else ["initial settings"]
        if cur and not changes:
            return cur[0], []
        write_settings_file(self.toml_path, new)
        v = (cur[0] + 1) if cur else 1
        self.db.execute("INSERT INTO versions(version, ts, author, note, diff, settings, file_hash) VALUES (?,?,?,?,?,?,?)",
                        (v, time.time(), author, note, orjson.dumps(changes).decode(),
                         orjson.dumps(new.to_dict()).decode(), self._hash(self.toml_path)))
        return v, changes

    def load_or_import(self) -> tuple[int, Settings, list[str]]:
        """Startup: create v1 from the file (or defaults), or import the file if it was edited by hand."""
        cur = self.current()
        file_exists = self.toml_path.exists()
        if cur is None:
            s = load_settings_file(self.toml_path) if file_exists else Settings()
            v, changes = self.save(s, "startup", "initial settings")
            return v, s, changes
        if file_exists and self._hash(self.toml_path) != self.last_file_hash():
            s = load_settings_file(self.toml_path)  # raises SettingsError with every problem listed
            v, changes = self.save(s, "file", "imported edited settings.toml")
            return v, s, changes
        if not file_exists:
            write_settings_file(self.toml_path, cur[1])
        return cur[0], cur[1], []

    def history(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT version, ts, author, note, diff FROM versions ORDER BY version DESC LIMIT ?",
                               (limit,)).fetchall()
        return [{"version": r[0], "ts": r[1], "author": r[2], "note": r[3], "changes": orjson.loads(r[4])} for r in rows]
