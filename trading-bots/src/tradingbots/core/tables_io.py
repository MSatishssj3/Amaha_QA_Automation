"""Parquet table storage: atomic writes by the owning bot, lock-free SQL reads for everyone.

Layout::

    data/tables/<table>/date=YYYY-MM-DD/part-<ts>.parquet   (append tables, compacted nightly)
    data/tables/<table>/date=YYYY-MM-DD/data.parquet        (replace tables, rewritten atomically)
    data/tables/<table>/symbol=XYZ/data.parquet              (per-symbol tables)
    data/tables/<table>/data.parquet                         (unpartitioned tables)

Files are written to ``*.parquet.tmp`` and renamed into place, so a reader never sees half a file.
Reads go through DuckDB views over the Parquet globs (``TableReader``); every query opens its own
in-memory DuckDB connection, which keeps reads thread-safe and free of locks.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from .schemas import TABLES, coerce

log = logging.getLogger(__name__)
_SAFE = re.compile(r"^[A-Za-z0-9_\-.&]+$")


def _safe(value: str) -> str:
    value = str(value)
    if not _SAFE.match(value):
        value = re.sub(r"[^A-Za-z0-9_\-.&]", "_", value)
    return value


class TableStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, table: str, pkey: str | None = None, pval: Any = None) -> Path:
        d = self.root / table
        if pkey:
            d = d / f"{pkey}={_safe(pval.isoformat() if isinstance(pval, date) else pval)}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _write_atomic(df: pl.DataFrame, path: Path) -> None:
        tmp = path.with_name(f".{path.stem}-{uuid.uuid4().hex[:8]}.parquet.tmp")
        df.write_parquet(tmp, compression="zstd", statistics=True)
        os.replace(tmp, path)

    def append(self, table: str, rows: list[dict] | pl.DataFrame, day: date | None = None) -> list[str]:
        """Append rows. Partitioned-by-date tables need ``day`` or a ``date`` column in the rows."""
        td = TABLES[table]
        frame = rows if isinstance(rows, pl.DataFrame) else (pl.from_dicts(rows, strict=False) if rows else None)
        if frame is None or frame.height == 0:
            return []
        written: list[str] = []
        if td.partition == "date":
            if "date" in frame.columns:
                groups = frame.partition_by("date", as_dict=True)
            else:
                if day is None:
                    raise ValueError(f"{table}: append needs day= or a date column")
                groups = {(day,): frame}
            for key, part in groups.items():
                d = key[0] if isinstance(key, tuple) else key
                path = self._dir(table, "date", d) / f"part-{time.time_ns()}-{uuid.uuid4().hex[:6]}.parquet"
                self._write_atomic(coerce(table, part.drop("date", strict=False)), path)
                written.append(str(d))
        else:
            path = self._dir(table) / f"part-{time.time_ns()}-{uuid.uuid4().hex[:6]}.parquet"
            self._write_atomic(coerce(table, frame), path)
            written.append("")
        return written

    def replace(self, table: str, rows: list[dict] | pl.DataFrame, partition_value: Any = None) -> str:
        """Atomically replace one partition (or the whole unpartitioned table) with ``rows``."""
        td = TABLES[table]
        df = coerce(table, rows)
        if td.partition:
            if partition_value is None:
                raise ValueError(f"{table} is partitioned by {td.partition}: give partition_value")
            d = self._dir(table, td.partition, partition_value)
        else:
            d = self._dir(table)
        target = d / "data.parquet"
        self._write_atomic(df, target)
        for old in d.glob("part-*.parquet"):  # replace semantics: nothing else in this partition
            old.unlink(missing_ok=True)
        return str(partition_value or "")

    def compact(self, table: str, partition_value: Any) -> int:
        """Merge an append partition's part files into one (run when nobody is writing it)."""
        td = TABLES[table]
        d = self._dir(table, td.partition, partition_value) if td.partition else self._dir(table)
        parts = sorted(d.glob("part-*.parquet"))
        if len(parts) <= 1:
            return 0
        frames = [pl.read_parquet(p) for p in parts]
        existing = d / "data.parquet"
        if existing.exists():
            frames.insert(0, pl.read_parquet(existing))
        merged = pl.concat([coerce(table, f) for f in frames], how="vertical_relaxed")
        self._write_atomic(merged, existing)
        for p in parts:
            p.unlink(missing_ok=True)
        return len(parts)

    def partitions(self, table: str) -> list[str]:
        d = self.root / table
        if not d.exists():
            return []
        return sorted(p.name.split("=", 1)[1] for p in d.iterdir() if p.is_dir() and "=" in p.name)

    def drop_partition(self, table: str, partition_value: Any) -> None:
        td = TABLES[table]
        d = self.root / table / f"{td.partition}={_safe(str(partition_value))}"
        if d.exists():
            for p in d.iterdir():
                p.unlink(missing_ok=True)
            d.rmdir()


class TableReader:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def available(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(d.name for d in self.root.iterdir()
                      if d.is_dir() and d.name in TABLES and next(d.rglob("*.parquet"), None) is not None)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        names = set(self.available())
        for name, td in TABLES.items():
            if name in names:
                glob = (self.root / name / "**" / "*.parquet").as_posix()
                con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{glob}', hive_partitioning=true, "
                            f"union_by_name=true)")
            else:  # empty view with the declared schema so queries against not-yet-written tables still work
                con.register(f"__empty_{name}", td.empty().to_arrow())
                con.execute(f"CREATE VIEW {name} AS SELECT * FROM __empty_{name}")
        return con

    def query(self, sql: str, params: list | tuple | None = None) -> pl.DataFrame:
        for attempt in (1, 2):
            con = self._connect()
            try:
                return con.sql(sql, params=list(params) if params else None).pl()
            except duckdb.IOException:
                if attempt == 2:
                    raise
                time.sleep(0.05)  # a file was replaced mid-read (compaction); retry once
            finally:
                con.close()
        raise RuntimeError("unreachable")

    def table(self, name: str, where: str = "", params: list | None = None, order: str = "",
              limit: int | None = None) -> pl.DataFrame:
        if name not in TABLES:
            raise KeyError(f"unknown table {name!r}")
        sql = f"SELECT * FROM {name}"
        if where:
            sql += f" WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def count(self, name: str) -> int:
        return int(self.query(f"SELECT COUNT(*) AS n FROM {name}")["n"][0])
