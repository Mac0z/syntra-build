"""Opaque, UUID-backed internal identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self
from uuid import UUID, uuid4

from syntra_build.domain.errors import DomainValidationError


@dataclass(frozen=True, slots=True)
class _UUIDIdentifier:
    value: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.value, UUID):
            raise DomainValidationError("identifier value must be a UUID")

    @classmethod
    def generate(cls) -> Self:
        """Generate a collision-resistant identifier."""
        return cls(uuid4())

    @classmethod
    def from_string(cls, value: str) -> Self:
        """Parse an explicit identifier for persistence and deterministic tests."""
        if not isinstance(value, str) or not value.strip():
            raise DomainValidationError("identifier must be a non-empty UUID string")
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError) as error:
            raise DomainValidationError(
                "identifier must be a valid UUID string"
            ) from error
        return cls(parsed)

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class ProjectId(_UUIDIdentifier):
    """Stable internal identity for a project."""


@dataclass(frozen=True, slots=True)
class MilestoneId(_UUIDIdentifier):
    """Stable internal identity for a milestone."""


@dataclass(frozen=True, slots=True)
class JobId(_UUIDIdentifier):
    """Stable internal identity for a durable job."""


@dataclass(frozen=True, slots=True)
class GateId(_UUIDIdentifier):
    """Stable internal identity for a human gate."""


@dataclass(frozen=True, slots=True)
class WorkflowEventId(_UUIDIdentifier):
    """Stable internal identity for a workflow event."""
