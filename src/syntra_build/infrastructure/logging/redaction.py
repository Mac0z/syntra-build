"""Bounded conversion and key-based redaction for diagnostic metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final
from uuid import UUID

from syntra_build.infrastructure.config import SecretInputs, SecretValue

REDACTED: Final = "[REDACTED]"
_MAX_DEPTH = 8
_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "apikey",
    "authorization",
    "privatekey",
    "credential",
)


def is_sensitive_key(key: object) -> bool:
    """Return whether a structured field name clearly denotes secret material."""
    normalised = "".join(
        character for character in str(key).lower() if character.isalnum()
    )
    return any(part in normalised for part in _SENSITIVE_KEY_PARTS)


def safe_value(value: object, *, _depth: int = 0) -> object:
    """Redact secrets and convert a bounded set of values into JSON-safe values."""
    if _depth >= _MAX_DEPTH:
        return "[MAX_DEPTH]"
    if isinstance(value, (SecretValue, SecretInputs)):
        return REDACTED
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, (Path, UUID)):
        return str(value)
    if isinstance(value, Enum):
        return safe_value(value.value, _depth=_depth + 1)
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED
            if is_sensitive_key(key)
            else safe_value(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [safe_value(item, _depth=_depth + 1) for item in value]
    return f"[UNSUPPORTED:{type(value).__name__}]"


def redact_mapping(values: Mapping[str, object]) -> dict[str, object]:
    """Return a JSON-safe copy with sensitive fields replaced."""
    converted = safe_value(values)
    if not isinstance(converted, dict):  # pragma: no cover - fixed by input type
        return {}
    return converted
