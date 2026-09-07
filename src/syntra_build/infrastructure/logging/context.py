"""Task-local diagnostic context for structured application logs."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class LoggingContext:
    """Identifiers shared by log records for one logical operation."""

    correlation_id: str | None = None
    project_id: str | None = None
    milestone_id: str | None = None
    job_id: str | None = None
    gate_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        """Return only identifiers that are present."""
        return {
            name: value
            for name, value in (
                ("correlation_id", self.correlation_id),
                ("project_id", self.project_id),
                ("milestone_id", self.milestone_id),
                ("job_id", self.job_id),
                ("gate_id", self.gate_id),
            )
            if value is not None
        }


_current_context: ContextVar[LoggingContext] = ContextVar(
    "syntra_logging_context", default=LoggingContext()
)


def new_correlation_id() -> str:
    """Create an opaque, log-safe correlation identifier."""
    return str(uuid4())


def get_logging_context() -> Mapping[str, str]:
    """Return a read-only view of the current task's populated context."""
    return _current_context.get().as_dict()


@contextmanager
def logging_context(
    *,
    correlation_id: str | None = None,
    project_id: str | None = None,
    milestone_id: str | None = None,
    job_id: str | None = None,
    gate_id: str | None = None,
) -> Iterator[LoggingContext]:
    """Establish a scope, inheriting unspecified identifiers from its parent.

    Passing ``None`` means "inherit". Use :func:`clear_logging_context` when a
    new operation must intentionally start with no inherited identifiers.
    """
    previous = _current_context.get()
    current = LoggingContext(
        correlation_id=correlation_id or previous.correlation_id,
        project_id=project_id or previous.project_id,
        milestone_id=milestone_id or previous.milestone_id,
        job_id=job_id or previous.job_id,
        gate_id=gate_id or previous.gate_id,
    )
    token = _current_context.set(current)
    try:
        yield current
    finally:
        _current_context.reset(token)


@contextmanager
def clear_logging_context() -> Iterator[None]:
    """Temporarily remove all context and restore it when the scope exits."""
    token = _current_context.set(LoggingContext())
    try:
        yield
    finally:
        _current_context.reset(token)
