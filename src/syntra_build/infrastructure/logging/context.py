"""Task-local logging context for correlating related operations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from uuid import uuid4

CONTEXT_FIELDS = (
    "correlation_id",
    "project_id",
    "milestone_id",
    "job_id",
    "gate_id",
)

_context: ContextVar[dict[str, str]] = ContextVar("syntra_logging_context", default={})
_UNSET = object()


def new_correlation_id() -> str:
    """Return a new opaque correlation identifier."""
    return str(uuid4())


def current_logging_context() -> Mapping[str, str]:
    """Return an immutable snapshot of the current task's logging context."""
    return MappingProxyType(_context.get().copy())


@contextmanager
def logging_context(
    *,
    correlation_id: str | None | object = _UNSET,
    project_id: str | None | object = _UNSET,
    milestone_id: str | None | object = _UNSET,
    job_id: str | None | object = _UNSET,
    gate_id: str | None | object = _UNSET,
) -> Iterator[None]:
    """Set scoped identifiers, restoring the previous context on exit.

    An omitted argument inherits its current value. Passing ``None`` explicitly
    clears that field for the scope.
    """
    updated = _context.get().copy()
    supplied = {
        "correlation_id": correlation_id,
        "project_id": project_id,
        "milestone_id": milestone_id,
        "job_id": job_id,
        "gate_id": gate_id,
    }
    for name, value in supplied.items():
        if value is _UNSET:
            continue
        if value is None:
            updated.pop(name, None)
        elif isinstance(value, str):
            updated[name] = value
        else:
            raise TypeError(f"{name} must be a string or None")

    token = _context.set(updated)
    try:
        yield
    finally:
        _context.reset(token)
