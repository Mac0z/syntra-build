"""Configuration and formatters for Syntra application logging."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from io import TextIOBase
from typing import Final

from syntra_build.infrastructure.config import ApplicationConfig
from syntra_build.infrastructure.logging.context import current_logging_context
from syntra_build.infrastructure.logging.redaction import (
    safe_format_message,
    safe_log_value,
    sanitize_diagnostic_text,
)

LOGGER_NAMESPACE: Final = "syntra_build"
_HANDLER_MARKER = "_syntra_build_handler"
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}


def _record_data(record: logging.LogRecord) -> dict[str, object]:
    message = safe_format_message(record.msg, record.args)
    event = safe_format_message(getattr(record, "event", record.msg), ())
    data: dict[str, object] = {
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "component": record.name,
        "event": event,
        "message": message,
    }
    data.update(current_logging_context())

    metadata = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_FIELDS
        and key not in {"event", *_record_context_fields()}
        and not key.startswith("_")
    }
    explicit_metadata = metadata.pop("metadata", None)
    if isinstance(explicit_metadata, Mapping):
        metadata.update(explicit_metadata)
    elif explicit_metadata is not None:
        metadata["metadata"] = explicit_metadata
    if metadata:
        data["metadata"] = safe_log_value(metadata)

    for field in _record_context_fields():
        if field in record.__dict__:
            data[field] = safe_log_value(record.__dict__[field])

    if record.exc_info:
        error_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        error_message = (
            sanitize_diagnostic_text(str(record.exc_info[1]))
            if record.exc_info[1]
            else ""
        )
        data["error"] = safe_log_value({"type": error_type, "message": error_message})
        data["exception"] = sanitize_diagnostic_text(self_format_exception(record))
    return data


def _record_context_fields() -> tuple[str, ...]:
    return ("correlation_id", "project_id", "milestone_id", "job_id", "gate_id")


def self_format_exception(record: logging.LogRecord) -> str:
    """Format an exception without inspecting or serialising local variables."""
    return (
        logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
    )


class JsonFormatter(logging.Formatter):
    """Emit one deterministic JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            _record_data(record),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class TextFormatter(logging.Formatter):
    """Human-readable development formatter with the same safe record pipeline."""

    def format(self, record: logging.LogRecord) -> str:
        data = _record_data(record)
        context = " ".join(
            f"{key}={value}"
            for key, value in data.items()
            if key
            not in {"timestamp", "level", "logger", "component", "event", "message"}
        )
        suffix = f" {context}" if context else ""
        return (
            f"{data['timestamp']} {data['level']} {data['logger']} "
            f"{data['event']}: {data['message']}{suffix}"
        )


def configure_logging(
    config: ApplicationConfig, *, stream: TextIOBase | None = None
) -> logging.Logger:
    """Configure and return the Syntra logger namespace from application config.

    Console output is intentional for systemd/journald. Reconfiguration replaces
    only handlers previously installed by this function.
    """
    logger = logging.getLogger(LOGGER_NAMESPACE)
    logger.setLevel(config.logging.level)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()

    handler = logging.StreamHandler(stream or sys.stderr)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setLevel(config.logging.level)
    formatter: logging.Formatter = (
        JsonFormatter() if config.logging.structured else TextFormatter()
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
