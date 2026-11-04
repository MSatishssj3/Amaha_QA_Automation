"""Non-blocking logging with secret redaction.

All handlers sit behind a ``QueueHandler`` so a log call on the hot path costs one queue put;
formatting and disk/console I/O happen on the listener thread.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import re
import sys
from datetime import datetime
from pathlib import Path

import orjson

from .clock import IST

_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWTs (Dhan access tokens)
    re.compile(r"(token=)[^&\s'\"]+", re.I),
    re.compile(r"(access-token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.I),
    re.compile(r"(pin=)[^&\s'\"]+", re.I),
    re.compile(r"(totp=)[^&\s'\"]+", re.I),
]

_secret_values: set[str] = set()


def register_secrets(values: list[str]) -> None:
    """Exact secret strings (token, PIN, TOTP secret ...) to scrub from every log line."""
    for v in values:
        if v and len(v) >= 4:
            _secret_values.add(v)


def redact(text: str) -> str:
    for v in _secret_values:
        if v in text:
            text = text.replace(v, "***")
    for pat in _PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        return datetime.fromtimestamp(record.created, IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, IST).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(orjson.dumps(payload).decode())


_listener: logging.handlers.QueueListener | None = None
_queue_handler: logging.Handler | None = None


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is *now* (survives stream swaps by test runners / daemons)."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property  # type: ignore[override]
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


def setup_logging(log_dir: Path | None = None, level: str = "INFO", console: bool = True) -> None:
    global _listener, _queue_handler
    shutdown_logging()
    handlers: list[logging.Handler] = []
    if console:
        h = _StderrHandler()
        h.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        handlers.append(h)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(log_dir / "dhanbot.jsonl", when="midnight", backupCount=30,
                                                       encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        handlers.append(fh)
    q: queue.SimpleQueue = queue.SimpleQueue()
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    _queue_handler = logging.handlers.QueueHandler(q)
    root.addHandler(_queue_handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    _listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    _listener.start()


def shutdown_logging() -> None:
    """Flush and detach the handlers installed by ``setup_logging`` (idempotent)."""
    global _listener, _queue_handler
    if _queue_handler is not None:
        logging.getLogger().removeHandler(_queue_handler)
        _queue_handler = None
    if _listener is not None:
        _listener.stop()
        for h in _listener.handlers:
            try:
                h.close()
            except Exception:
                pass
        _listener = None
