"""Structured logging with rotating file output and an in-memory ring buffer.

The Rich dashboard renders recent log records itself, so automation logs are
kept out of the live console region. A plain console handler is used when the
dashboard is disabled (non-interactive runs).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including arbitrary ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


class RingBufferHandler(logging.Handler):
    """Keeps recent formatted records for the terminal dashboard."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(
                {
                    "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
            )
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)

    def tail(self, count: int = 8) -> list[dict[str, Any]]:
        return list(self.records)[-count:]


_ring_handler: RingBufferHandler | None = None
_installed_handlers: list[logging.Handler] = []


def configure_logging(
    *,
    log_dir: Path,
    log_level: str = "INFO",
    log_json: bool = False,
    console: bool = True,
    capacity: int = 200,
) -> RingBufferHandler:
    """Configure the root logger once and return the ring buffer handler."""

    global _ring_handler

    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(log_level.upper())

    for handler in list(_installed_handlers):
        root.removeHandler(handler)
        handler.close()
    _installed_handlers.clear()

    formatter: logging.Formatter = JsonFormatter() if log_json else TextFormatter()

    file_handler = RotatingFileHandler(
        log_dir / "automation.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    _installed_handlers.append(file_handler)

    ring = RingBufferHandler(capacity=capacity)
    root.addHandler(ring)
    _installed_handlers.append(ring)
    _ring_handler = ring

    if console:
        rich_handler = RichHandler(
            console=Console(stderr=False),
            show_path=False,
            show_time=True,
            rich_tracebacks=True,
            markup=False,
        )
        rich_handler.setLevel(logging.INFO)
        root.addHandler(rich_handler)
        _installed_handlers.append(rich_handler)

    # Playwright is very chatty at DEBUG level.
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return ring


def get_ring_handler() -> RingBufferHandler | None:
    return _ring_handler


def recent_records(count: int = 8) -> list[dict[str, Any]]:
    if _ring_handler is None:
        return []
    return _ring_handler.tail(count)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
