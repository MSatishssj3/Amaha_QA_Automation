"""The message bus that lets the bots work together.

Three interchangeable backends behind one interface (all pass the same contract tests):

* ``SqliteBus`` (default) - a WAL-mode SQLite file shared by every process on one machine. No extra
  software, works on Windows and Linux. Consumers poll every 50 ms.
* ``RedisBus`` - Redis Streams with one consumer group per bot (lower latency, high throughput).
* ``MemoryBus`` - in-process, for tests and single-process runs.

Semantics
* **Events**: ``publish(topic, data)``. Each *group* (one per bot) receives every message of the
  topics it subscribed to, in order, at least once. A group's read position survives restarts, so a
  restarted bot continues where it stopped. New groups start at "latest" unless asked otherwise.
* **Commands**: ``request(topic, data, timeout)`` publishes with a correlation id; the serving bot
  replies with ``send_reply``. Topics ``cmd.<bot>.<action>``.
* **Key-value** with optional TTL, for state other bots need instantly (settings, latest quotes,
  heartbeats, budget policy).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Message:
    id: str
    topic: str
    ts: float
    source: str
    data: dict[str, Any]


class BusTimeout(Exception):
    pass


class BusError(Exception):
    pass


def _dumps(obj: Any) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY, default=str)


class Bus(ABC):
    def __init__(self, client: str) -> None:
        self.client = client  # bot / process name, used as message source

    async def connect(self) -> None:  # noqa: B027 - optional hook
        pass

    async def close(self) -> None:  # noqa: B027 - optional hook
        pass

    # ---- events ---------------------------------------------------------------------------------
    @abstractmethod
    async def publish(self, topic: str, data: dict[str, Any]) -> str: ...

    @abstractmethod
    async def ensure_group(self, group: str, topics: list[str], start: str = "latest") -> None:
        """Create the group's read position now (so nothing published from here on is missed)."""

    @abstractmethod
    async def consume(self, group: str, topics: list[str], *, batch: int = 200, block_s: float = 1.0) -> list[Message]:
        """Wait up to ``block_s`` for messages after the group's position. Call ``ack`` after processing."""

    @abstractmethod
    async def ack(self, group: str, msgs: list[Message]) -> None: ...

    @abstractmethod
    async def recent(self, topic: str, limit: int = 100) -> list[Message]:
        """The newest retained messages on ``topic``, oldest first. A read-only peek: no consumer group,
        no cursor (dashboards use it to show recent alerts)."""

    # ---- key-value --------------------------------------------------------------------------------
    @abstractmethod
    async def kv_set(self, key: str, value: Any, ttl: float | None = None) -> None: ...

    @abstractmethod
    async def kv_get(self, key: str, default: Any = None) -> Any: ...

    @abstractmethod
    async def kv_delete(self, key: str) -> None: ...

    @abstractmethod
    async def kv_keys(self, prefix: str) -> list[str]: ...

    # ---- request / reply ------------------------------------------------------------------------------
    @abstractmethod
    async def send_reply(self, corr: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    async def wait_reply(self, corr: str, timeout: float) -> dict[str, Any] | None: ...

    async def request(self, topic: str, data: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        corr = uuid.uuid4().hex
        await self.publish(topic, {**(data or {}), "_corr": corr})
        reply = await self.wait_reply(corr, timeout)
        if reply is None:
            raise BusTimeout(f"no reply to {topic} within {timeout:.0f}s (is that bot running?)")
        return reply

    # ---- maintenance ---------------------------------------------------------------------------------
    @abstractmethod
    async def trim(self, retention_s: float) -> int:
        """Delete messages older than ``retention_s`` and expired keys. Returns rows removed."""


# =================================================================================================
# memory
# =================================================================================================


@dataclass
class _MemState:
    messages: list[Message] = field(default_factory=list)
    seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    cursors: dict[str, int] = field(default_factory=dict)
    kv: dict[str, tuple[Any, float | None]] = field(default_factory=dict)
    replies: dict[str, dict] = field(default_factory=dict)
    cond: asyncio.Condition | None = None


class MemoryBus(Bus):
    """In-process bus. All ``MemoryBus`` objects with the same namespace share one state."""

    _namespaces: dict[str, _MemState] = {}

    def __init__(self, client: str, namespace: str = "default") -> None:
        super().__init__(client)
        self.ns = namespace
        self.state = MemoryBus._namespaces.setdefault(namespace, _MemState())

    @classmethod
    def reset(cls, namespace: str | None = None) -> None:
        if namespace is None:
            cls._namespaces.clear()
        else:
            cls._namespaces.pop(namespace, None)

    def _cond(self) -> asyncio.Condition:
        if self.state.cond is None:
            self.state.cond = asyncio.Condition()
        return self.state.cond

    async def publish(self, topic: str, data: dict[str, Any]) -> str:
        n = next(self.state.seq)
        # round-trip through JSON so the memory bus behaves exactly like the persistent ones
        self.state.messages.append(Message(str(n), topic, time.time(), self.client, orjson.loads(_dumps(data))))
        async with self._cond():
            self._cond().notify_all()
        return str(n)

    async def ensure_group(self, group: str, topics: list[str], start: str = "latest") -> None:
        if group not in self.state.cursors:
            last = int(self.state.messages[-1].id) if self.state.messages else 0
            self.state.cursors[group] = last if start == "latest" else 0

    def _pending(self, group: str, topics: list[str], batch: int) -> list[Message]:
        cur = self.state.cursors.get(group, 0)
        ts = set(topics)
        out = []
        for m in self.state.messages:
            if int(m.id) > cur and m.topic in ts:
                out.append(m)
                if len(out) >= batch:
                    break
        return out

    async def consume(self, group: str, topics: list[str], *, batch: int = 200, block_s: float = 1.0) -> list[Message]:
        await self.ensure_group(group, topics)
        out = self._pending(group, topics, batch)
        if out:
            return out
        try:
            async with self._cond():
                await asyncio.wait_for(self._cond().wait_for(lambda: bool(self._pending(group, topics, 1))), block_s)
        except asyncio.TimeoutError:
            return []
        return self._pending(group, topics, batch)

    async def ack(self, group: str, msgs: list[Message]) -> None:
        if msgs:
            self.state.cursors[group] = max(self.state.cursors.get(group, 0), max(int(m.id) for m in msgs))

    async def recent(self, topic: str, limit: int = 100) -> list[Message]:
        return [m for m in self.state.messages if m.topic == topic][-limit:] if limit > 0 else []

    async def kv_set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self.state.kv[key] = (orjson.loads(_dumps(value)), time.time() + ttl if ttl else None)

    async def kv_get(self, key: str, default: Any = None) -> Any:
        item = self.state.kv.get(key)
        if item is None or (item[1] is not None and item[1] < time.time()):
            return default
        return item[0]

    async def kv_delete(self, key: str) -> None:
        self.state.kv.pop(key, None)

    async def kv_keys(self, prefix: str) -> list[str]:
        now = time.time()
        return sorted(k for k, (_, exp) in self.state.kv.items() if k.startswith(prefix) and (exp is None or exp >= now))

    async def send_reply(self, corr: str, data: dict[str, Any]) -> None:
        self.state.replies[corr] = orjson.loads(_dumps(data))
        async with self._cond():
            self._cond().notify_all()

    async def wait_reply(self, corr: str, timeout: float) -> dict[str, Any] | None:
        try:
            async with self._cond():
                await asyncio.wait_for(self._cond().wait_for(lambda: corr in self.state.replies), timeout)
        except asyncio.TimeoutError:
            return None
        return self.state.replies.pop(corr)

    async def trim(self, retention_s: float) -> int:
        cut = time.time() - retention_s
        before = len(self.state.messages)
        self.state.messages = [m for m in self.state.messages if m.ts >= cut]
        return before - len(self.state.messages)


# =================================================================================================
# sqlite
# =================================================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, ts REAL NOT NULL,
    source TEXT NOT NULL, data BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS ix_messages_topic ON messages(topic, id);
CREATE INDEX IF NOT EXISTS ix_messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS cursors (grp TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL);
CREATE TABLE IF NOT EXISTS replies (corr TEXT PRIMARY KEY, data BLOB NOT NULL, ts REAL NOT NULL);
"""


class SqliteBus(Bus):
    def __init__(self, path: Path, client: str, poll_s: float = 0.05) -> None:
        super().__init__(client)
        self.path = Path(path)
        self.poll_s = poll_s
        self._pool: ThreadPoolExecutor | None = None
        self._conn: sqlite3.Connection | None = None
        self._cursor_cache: dict[str, int] = {}

    # every DB call runs on one private thread: never blocks the event loop, one connection
    async def _run(self, fn, *args):
        if self._pool is None:
            raise BusError("SqliteBus.connect() not called")
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(_SCHEMA)
        self._conn = conn
        try:
            os.chmod(self.path, 0o600)  # holds the access token: owner-only
        except OSError:
            pass

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"bus-{self.client}")
            await self._run(self._open)

    async def close(self) -> None:
        if self._pool is not None:
            if self._conn is not None:
                await self._run(self._conn.close)
            self._pool.shutdown(wait=True)
            self._pool = None
            self._conn = None

    @property
    def c(self) -> sqlite3.Connection:
        assert self._conn is not None
        return self._conn

    def _write(self, sql: str, params: tuple) -> int:
        cur = self.c.execute(sql, params)
        return int(cur.lastrowid or 0)

    async def publish(self, topic: str, data: dict[str, Any]) -> str:
        rid = await self._run(self._write, "INSERT INTO messages(topic, ts, source, data) VALUES (?,?,?,?)",
                              (topic, time.time(), self.client, _dumps(data)))
        return str(rid)

    def _ensure(self, group: str, start: str) -> int:
        row = self.c.execute("SELECT last_id FROM cursors WHERE grp=?", (group,)).fetchone()
        if row is not None:
            return int(row[0])
        if start == "latest":
            last = self.c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        else:
            last = 0
        self.c.execute("INSERT OR IGNORE INTO cursors(grp, last_id) VALUES (?,?)", (group, int(last)))
        return int(last)

    async def ensure_group(self, group: str, topics: list[str], start: str = "latest") -> None:
        if group not in self._cursor_cache:
            self._cursor_cache[group] = await self._run(self._ensure, group, start)

    def _fetch(self, last: int, topics: list[str], batch: int) -> list[tuple]:
        q = ",".join("?" * len(topics))
        return self.c.execute(f"SELECT id, topic, ts, source, data FROM messages WHERE id > ? AND topic IN ({q}) "
                              f"ORDER BY id LIMIT ?", (last, *topics, batch)).fetchall()

    async def consume(self, group: str, topics: list[str], *, batch: int = 200, block_s: float = 1.0) -> list[Message]:
        await self.ensure_group(group, topics)
        deadline = time.monotonic() + block_s
        while True:
            rows = await self._run(self._fetch, self._cursor_cache[group], topics, batch)
            if rows:
                return [Message(str(r[0]), r[1], r[2], r[3], orjson.loads(r[4])) for r in rows]
            if time.monotonic() >= deadline:
                return []
            await asyncio.sleep(self.poll_s)

    async def ack(self, group: str, msgs: list[Message]) -> None:
        if not msgs:
            return
        last = max(int(m.id) for m in msgs)
        self._cursor_cache[group] = max(self._cursor_cache.get(group, 0), last)
        await self._run(self._write, "INSERT INTO cursors(grp, last_id) VALUES (?,?) "
                        "ON CONFLICT(grp) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)", (group, last))

    def _recent(self, topic: str, limit: int) -> list[tuple]:
        return self.c.execute("SELECT id, topic, ts, source, data FROM messages WHERE topic = ? ORDER BY id DESC "
                              "LIMIT ?", (topic, limit)).fetchall()

    async def recent(self, topic: str, limit: int = 100) -> list[Message]:
        rows = await self._run(self._recent, topic, max(0, int(limit)))
        return [Message(str(r[0]), r[1], r[2], r[3], orjson.loads(r[4])) for r in reversed(rows)]

    async def kv_set(self, key: str, value: Any, ttl: float | None = None) -> None:
        await self._run(self._write, "INSERT OR REPLACE INTO kv(key, value, expires) VALUES (?,?,?)",
                        (key, _dumps(value), time.time() + ttl if ttl else None))

    def _kv_get(self, key: str) -> tuple | None:
        return self.c.execute("SELECT value, expires FROM kv WHERE key=?", (key,)).fetchone()

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self._run(self._kv_get, key)
        if row is None or (row[1] is not None and row[1] < time.time()):
            return default
        return orjson.loads(row[0])

    async def kv_delete(self, key: str) -> None:
        await self._run(self._write, "DELETE FROM kv WHERE key=?", (key,))

    def _kv_keys(self, prefix: str) -> list[str]:
        rows = self.c.execute("SELECT key FROM kv WHERE key >= ? AND key < ? AND (expires IS NULL OR expires >= ?) "
                              "ORDER BY key", (prefix, prefix + "￿", time.time())).fetchall()
        return [r[0] for r in rows]

    async def kv_keys(self, prefix: str) -> list[str]:
        return await self._run(self._kv_keys, prefix)

    async def send_reply(self, corr: str, data: dict[str, Any]) -> None:
        await self._run(self._write, "INSERT OR REPLACE INTO replies(corr, data, ts) VALUES (?,?,?)",
                        (corr, _dumps(data), time.time()))

    def _take_reply(self, corr: str) -> bytes | None:
        row = self.c.execute("SELECT data FROM replies WHERE corr=?", (corr,)).fetchone()
        if row is None:
            return None
        self.c.execute("DELETE FROM replies WHERE corr=?", (corr,))
        return row[0]

    async def wait_reply(self, corr: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            raw = await self._run(self._take_reply, corr)
            if raw is not None:
                return orjson.loads(raw)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.02)

    def _trim(self, cut: float) -> int:
        n = self.c.execute("DELETE FROM messages WHERE ts < ?", (cut,)).rowcount
        self.c.execute("DELETE FROM kv WHERE expires IS NOT NULL AND expires < ?", (time.time(),))
        self.c.execute("DELETE FROM replies WHERE ts < ?", (time.time() - 300,))
        return n

    async def trim(self, retention_s: float) -> int:
        return await self._run(self._trim, time.time() - retention_s)


# =================================================================================================
# redis
# =================================================================================================


class RedisBus(Bus):
    PREFIX = "tb:"

    def __init__(self, url: str, client: str, maxlen: int = 200_000) -> None:
        super().__init__(client)
        self.url = url
        self.maxlen = maxlen
        self.r = None
        self._groups: set[tuple[str, str]] = set()
        self._pending_done: set[str] = set()

    def _s(self, topic: str) -> str:
        return f"{self.PREFIX}s:{topic}"

    async def connect(self) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as e:  # pragma: no cover - optional dependency
            raise BusError("bus_backend = 'redis' needs: pip install 'tradingbots[redis]'") from e
        self.r = aioredis.from_url(self.url)
        await self.r.ping()

    async def close(self) -> None:
        if self.r is not None:
            await self.r.aclose()
            self.r = None

    async def publish(self, topic: str, data: dict[str, Any]) -> str:
        rid = await self.r.xadd(self._s(topic), {"d": _dumps(data), "src": self.client, "ts": repr(time.time())},
                                maxlen=self.maxlen, approximate=True)
        return rid.decode() if isinstance(rid, bytes) else str(rid)

    async def ensure_group(self, group: str, topics: list[str], start: str = "latest") -> None:
        from redis.exceptions import ResponseError

        for t in topics:
            if (group, t) in self._groups:
                continue
            try:
                await self.r.xgroup_create(self._s(t), group, id="$" if start == "latest" else "0", mkstream=True)
            except ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
            self._groups.add((group, t))

    async def consume(self, group: str, topics: list[str], *, batch: int = 200, block_s: float = 1.0) -> list[Message]:
        await self.ensure_group(group, topics)
        # after a restart, first re-deliver what this group read but never acknowledged
        since = "0" if group not in self._pending_done else ">"
        resp = await self.r.xreadgroup(group, group, {self._s(t): since for t in topics}, count=batch,
                                       block=None if since == "0" else int(block_s * 1000))
        out: list[Message] = []
        for stream, entries in resp or []:
            topic = (stream.decode() if isinstance(stream, bytes) else stream)[len(self.PREFIX) + 2:]
            for rid, fields in entries:
                f = {(k.decode() if isinstance(k, bytes) else k): v for k, v in fields.items()}
                out.append(Message(rid.decode() if isinstance(rid, bytes) else rid, topic, float(f["ts"]),
                                   f["src"].decode() if isinstance(f["src"], bytes) else f["src"], orjson.loads(f["d"])))
        if since == "0" and not out:
            self._pending_done.add(group)
            return await self.consume(group, topics, batch=batch, block_s=block_s)
        out.sort(key=lambda m: (m.ts, m.id))
        return out

    async def ack(self, group: str, msgs: list[Message]) -> None:
        by_topic: dict[str, list[str]] = {}
        for m in msgs:
            by_topic.setdefault(m.topic, []).append(m.id)
        for t, ids in by_topic.items():
            await self.r.xack(self._s(t), group, *ids)

    async def recent(self, topic: str, limit: int = 100) -> list[Message]:
        if limit <= 0:
            return []
        entries = await self.r.xrevrange(self._s(topic), "+", "-", count=int(limit))
        out = []
        for rid, fields in reversed(entries or []):
            f = {(k.decode() if isinstance(k, bytes) else k): v for k, v in fields.items()}
            out.append(Message(rid.decode() if isinstance(rid, bytes) else rid, topic, float(f["ts"]),
                               f["src"].decode() if isinstance(f["src"], bytes) else f["src"], orjson.loads(f["d"])))
        return out

    async def kv_set(self, key: str, value: Any, ttl: float | None = None) -> None:
        await self.r.set(f"{self.PREFIX}kv:{key}", _dumps(value), px=int(ttl * 1000) if ttl else None)

    async def kv_get(self, key: str, default: Any = None) -> Any:
        raw = await self.r.get(f"{self.PREFIX}kv:{key}")
        return default if raw is None else orjson.loads(raw)

    async def kv_delete(self, key: str) -> None:
        await self.r.delete(f"{self.PREFIX}kv:{key}")

    async def kv_keys(self, prefix: str) -> list[str]:
        base = f"{self.PREFIX}kv:"
        keys = [k.decode() if isinstance(k, bytes) else k async for k in self.r.scan_iter(f"{base}{prefix}*")]
        return sorted(k[len(base):] for k in keys)

    async def send_reply(self, corr: str, data: dict[str, Any]) -> None:
        key = f"{self.PREFIX}r:{corr}"
        await self.r.rpush(key, _dumps(data))
        await self.r.expire(key, 300)

    async def wait_reply(self, corr: str, timeout: float) -> dict[str, Any] | None:
        res = await self.r.blpop([f"{self.PREFIX}r:{corr}"], timeout=max(1, int(round(timeout))))
        return None if res is None else orjson.loads(res[1])

    async def trim(self, retention_s: float) -> int:
        min_id = f"{int((time.time() - retention_s) * 1000)}-0"
        n = 0
        async for k in self.r.scan_iter(f"{self.PREFIX}s:*"):
            n += await self.r.xtrim(k, minid=min_id, approximate=True)
        return n


def make_bus(backend: str, client: str, *, data_dir: Path, redis_url: str = "", namespace: str = "default") -> Bus:
    if backend == "sqlite":
        return SqliteBus(Path(data_dir) / "bus.db", client)
    if backend == "redis":
        return RedisBus(redis_url, client)
    if backend == "memory":
        return MemoryBus(client, namespace)
    raise BusError(f"unknown bus backend {backend!r}")
