"""V2 observability helpers.

A single ``configure_logging`` factory sets up a JSON-line logger that
emits to a configurable sink (defaults to stderr). The ``log_event``
helper formats structured records with a stable shape:

    {
        "timestamp": ISO-8601,
        "level": "INFO",
        "event": "ingest.created",
        "...": any extra kwargs
    }
"""
from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from typing import IO, Any

_LOGGER_NAME = "aftergraph.work-intelligence"


def _format(record: logging.LogRecord) -> str:
    payload: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "event": getattr(record, "event", None) or record.getMessage(),
    }
    extra = getattr(record, "extra_fields", None)
    if isinstance(extra, dict):
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(
    sink: IO[str] | None = None,
    level: int = logging.INFO,
    name: str = _LOGGER_NAME,
) -> logging.Logger:
    """Configure the work-intelligence JSON logger.

    Idempotent: re-calling clears existing handlers.
    """
    logger = logging.getLogger(name)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.StreamHandler(sink if sink is not None else io.StringIO())
    handler.setFormatter(logging.Formatter("%(message)s"))
    # Wrap the formatter to emit JSON.
    def _emit(rec):  # type: ignore
        try:
            handler.emit(rec)
            return _format(rec)
        except Exception:
            return ""

    # We piggyback on logging.Formatter; override format.
    handler.setFormatter(logging.Formatter())
    orig_format = handler.format

    def json_format(record: logging.LogRecord) -> str:  # type: ignore
        try:
            return _format(record)
        except Exception:
            return orig_format(record)

    handler.setFormatter(logging.Formatter())
    handler.format = json_format  # type: ignore
    logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Log a structured event."""
    record_kwargs = {"event": event, **fields}
    logger.info(event, extra={"extra_fields": record_kwargs})


__all__ = ["configure_logging", "log_event"]