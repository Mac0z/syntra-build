"""Immutable workflow-event envelope and typed processing contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_timestamp_order,
    require_utc,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import (
    GateId,
    JobId,
    MilestoneId,
    ProjectId,
    WorkflowEventId,
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type FrozenJsonValue = (
    JsonScalar | tuple[FrozenJsonValue, ...] | Mapping[str, FrozenJsonValue]
)


class WorkflowEventSource(StrEnum):
    INTERNAL = "INTERNAL"
    TELEGRAM = "TELEGRAM"
    GITHUB = "GITHUB"
    CI = "CI"
    ARCHITECT = "ARCHITECT"
    CODEX = "CODEX"
    RECOVERY = "RECOVERY"
    SYSTEM = "SYSTEM"

    @property
    def requires_deduplication_key(self) -> bool:
        return self in {
            self.TELEGRAM,
            self.GITHUB,
            self.CI,
            self.ARCHITECT,
            self.CODEX,
        }


class EventProcessingStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


def _freeze_json(value: object, path: str = "payload") -> FrozenJsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        try:
            json.dumps(value, allow_nan=False)
        except ValueError as error:
            raise DomainValidationError(
                f"{path} must contain finite numbers"
            ) from error
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise DomainValidationError(f"{path} keys must be strings")
            frozen[key] = _freeze_json(child, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(child, f"{path}[]") for child in value)
    raise DomainValidationError(f"{path} must contain only JSON values")


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    id: WorkflowEventId
    project_id: ProjectId
    event_type: str
    occurred_at: datetime
    correlation_id: str
    milestone_id: MilestoneId | None = None
    job_id: JobId | None = None
    gate_id: GateId | None = None
    payload: Mapping[str, object] | None = None
    source: WorkflowEventSource = WorkflowEventSource.INTERNAL
    received_at: datetime | None = None
    causation_event_id: WorkflowEventId | None = None
    external_deduplication_key: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, WorkflowEventId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        for value, expected, name in (
            (self.milestone_id, MilestoneId, "milestone_id"),
            (self.job_id, JobId, "job_id"),
            (self.gate_id, GateId, "gate_id"),
            (self.causation_event_id, WorkflowEventId, "causation_event_id"),
        ):
            if value is not None:
                require_identifier(value, expected, name)
        require_text(self.event_type, "event_type")
        require_text(self.correlation_id, "correlation_id")
        require_enum(self.source, WorkflowEventSource, "source")
        require_utc(self.occurred_at, "occurred_at")
        received_at = self.received_at or self.occurred_at
        require_utc(received_at, "received_at")
        require_timestamp_order(
            self.occurred_at, received_at, "occurred_at", "received_at"
        )
        object.__setattr__(self, "received_at", received_at)
        if self.causation_event_id == self.id:
            raise DomainValidationError("an event cannot cause itself")
        if self.external_deduplication_key is not None:
            require_text(self.external_deduplication_key, "external_deduplication_key")
        if (
            self.source.requires_deduplication_key
            and self.external_deduplication_key is None
        ):
            raise DomainValidationError(
                "externally sourced events require a deduplication key"
            )
        if self.payload is None:
            object.__setattr__(self, "payload", MappingProxyType({}))
        elif not isinstance(self.payload, Mapping):
            raise DomainValidationError("payload must be a mapping")
        else:
            object.__setattr__(self, "payload", _freeze_json(self.payload))


@dataclass(frozen=True, slots=True)
class PersistedWorkflowEvent:
    event: WorkflowEvent
    processing_status: EventProcessingStatus
    processing_attempt_count: int
    processed_at: datetime | None = None
    last_processing_error: str | None = None

    def __post_init__(self) -> None:
        require_enum(self.processing_status, EventProcessingStatus, "processing_status")
        if self.processing_attempt_count < 0:
            raise DomainValidationError("processing_attempt_count cannot be negative")
        if self.processed_at is not None:
            require_utc(self.processed_at, "processed_at")


@dataclass(frozen=True, slots=True)
class InsertedEvent:
    record: PersistedWorkflowEvent


@dataclass(frozen=True, slots=True)
class DuplicateEvent:
    record: PersistedWorkflowEvent


type EventInsertionResult = InsertedEvent | DuplicateEvent
