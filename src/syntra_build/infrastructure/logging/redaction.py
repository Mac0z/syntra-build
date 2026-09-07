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
_SENSITIVE_KEY = re.compile(
    r"(?:^|[^a-z0-9])(password|secret|token|api[_-]?key|authorization|"
    r"private[_-]?key|credential)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


def is_sensitive_key(key: object) -> bool:
    """Return whether a structured field name denotes secret material."""
    return bool(_SENSITIVE_KEY.search(str(key)))


def safe_log_value(value: object) -> object:
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
        return safe_log_value(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if is_sensitive_key(key) else safe_log_value(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [safe_log_value(item) for item in value]
    return f"[UNSERIALIZABLE:{type(value).__name__}]"
