"""Bounded, reusable conversion and redaction for log metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Final
from uuid import UUID

from syntra_build.infrastructure.config import SecretInputs, SecretValue

REDACTED: Final = "[REDACTED]"
MAX_DEPTH_MARKER: Final = "[MAX_DEPTH]"
FORMATTING_ERROR_MARKER: Final = "[FORMATTING_ERROR]"
MAX_LOG_VALUE_DEPTH: Final = 8
_SENSITIVE_KEY = re.compile(
    r"(?:^|[^a-z0-9])(password|secret|token|api[_-]?key|authorization|"
    r"private[_-]?key|credential)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"(?P<prefix>\b(?:password|secret|token|api[_-]?key|authorization|"
    r"private[_-]?key|credential)\b\s*[:=]\s*)"
    r"(?:(?:bearer)\s+)?"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)


def is_sensitive_key(key: object) -> bool:
    """Return whether a structured field name denotes secret material."""
    return bool(_SENSITIVE_KEY.search(str(key)))


def sanitize_diagnostic_text(value: str) -> str:
    """Redact values in common key/value credential forms within diagnostic text."""
    return _SENSITIVE_TEXT.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", value
    )


def safe_format_message(message: object, arguments: object) -> str:
    """Apply logging-style interpolation only after arguments have been made safe."""
    safe_message = safe_log_value(message)
    template = safe_message if isinstance(safe_message, str) else str(safe_message)
    if not arguments:
        return sanitize_diagnostic_text(template)

    safe_arguments = safe_log_value(arguments)
    if isinstance(arguments, tuple) and isinstance(safe_arguments, list):
        interpolation_arguments: object = tuple(safe_arguments)
    else:
        interpolation_arguments = safe_arguments
    try:
        formatted = template % interpolation_arguments
    except KeyError, OverflowError, TypeError, ValueError:
        return f"{sanitize_diagnostic_text(template)} {FORMATTING_ERROR_MARKER}"
    return sanitize_diagnostic_text(formatted)


def safe_log_value(value: object, *, _depth: int = 0) -> object:
    """Redact secrets and convert only an allow-list of safe value types."""
    if isinstance(value, (SecretValue, SecretInputs)):
        return REDACTED
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).isoformat()
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (Path, UUID)):
        return str(value)
    if isinstance(value, Enum):
        return safe_log_value(value.value, _depth=_depth)
    if _depth >= MAX_LOG_VALUE_DEPTH:
        return MAX_DEPTH_MARKER
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED
            if is_sensitive_key(key)
            else safe_log_value(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [safe_log_value(item, _depth=_depth + 1) for item in value]
    return f"[UNSERIALIZABLE:{type(value).__name__}]"
