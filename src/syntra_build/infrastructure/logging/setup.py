"""Deterministic standard-library logging configuration."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, TextIO, cast

from syntra_build.infrastructure.config import ApplicationConfig, LoggingConfig
from syntra_build.infrastructure.logging.context import get_logging_context
from syntra_build.infrastructure.logging.redaction import redact_mapping, safe_value

LOGGER_NAMESPACE: Final = "syntra_build"
_HANDLER_MARKER = "_syntra_build_handler"
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}


def _timestamp(record: logging.LogRecord) -> str:
    return (
        datetime.fromtimestamp(record.created, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class StructuredJsonFormatter(logging.Formatter):
    """Render a log record as one secret-safe JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        message = _safe_message(record)
        event_value = getattr(record, "event", None)
        event = event_value if isinstance(event_value, str) else message
        payload: dict[str, object] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "component": getattr(record, "component", record.name),
            "event": event,
            "message": message,
        }
        payload.update(get_logging_context())
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_FIELDS
                and key not in payload
                and key not in {"event", "component"}
            }
        )
        if record.exc_info:
            exc_info = cast(
                tuple[type[BaseException], BaseException, TracebackType | None],
                record.exc_info,
            )
            payload["error"] = _exception_details(exc_info, self)
        return json.dumps(
            redact_mapping(payload),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


def _safe_message(record: logging.LogRecord) -> str:
    """Format a record without exposing keyed secrets in message arguments."""
    message = safe_value(record.msg)
    if not isinstance(message, str):
        message = json.dumps(message, ensure_ascii=True, sort_keys=True)
    if not record.args:
        return message
    if isinstance(record.args, dict):
        arguments: object = redact_mapping(record.args)
    else:
        arguments = tuple(safe_value(value) for value in record.args)
    try:
        return message % arguments
    except TypeError, ValueError, KeyError:
        return f"{message} [LOG_ARGUMENTS_INVALID]"


def _exception_details(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
    formatter: logging.Formatter,
) -> dict[str, str]:
    exception_type, exception, _ = exc_info
    return {
        "type": exception_type.__name__,
        "message": str(exception),
        "stack_trace": formatter.formatException(exc_info),
    }


class SafeTextFormatter(logging.Formatter):
    """Development formatter that still applies metadata redaction."""

    def format(self, record: logging.LogRecord) -> str:
        structured = StructuredJsonFormatter().format(record)
        payload = json.loads(structured)
        context = " ".join(
            f"{key}={value}"
            for key in (
                "correlation_id",
                "project_id",
                "milestone_id",
                "job_id",
                "gate_id",
            )
            if (value := payload.get(key)) is not None
        )
        suffix = f" {context}" if context else ""
        return (
            f"{payload['timestamp']} {payload['level']} "
            f"{payload['component']} {payload['event']}: "
            f"{payload['message']}{suffix}"
        )


def configure_logging(
    config: ApplicationConfig | LoggingConfig,
    *,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure and return the Syntra application logger namespace.

    Production defaults to stderr for systemd/journald capture. ``stream`` is
    injectable so tests and embedding applications need not touch host paths.
    Reconfiguration replaces only handlers owned by this function.
    """
    logging_config = config.logging if isinstance(config, ApplicationConfig) else config
    logger = logging.getLogger(LOGGER_NAMESPACE)
    logger.setLevel(logging_config.level)
    logger.propagate = False

    for handler in tuple(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()

    handler = logging.StreamHandler(stream or sys.stderr)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setLevel(logging_config.level)
    handler.setFormatter(
        StructuredJsonFormatter() if logging_config.structured else SafeTextFormatter()
    )
    logger.addHandler(handler)
    return logger
