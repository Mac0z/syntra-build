"""Structured, contextual and redacting application logging."""

from syntra_build.infrastructure.logging.context import (
    current_logging_context,
    logging_context,
    new_correlation_id,
)
from syntra_build.infrastructure.logging.redaction import (
    MAX_DEPTH_MARKER,
    REDACTED,
    safe_format_message,
    safe_log_value,
    sanitize_diagnostic_text,
)
from syntra_build.infrastructure.logging.setup import configure_logging

__all__ = [
    "MAX_DEPTH_MARKER",
    "REDACTED",
    "configure_logging",
    "current_logging_context",
    "logging_context",
    "new_correlation_id",
    "safe_format_message",
    "safe_log_value",
    "sanitize_diagnostic_text",
]
