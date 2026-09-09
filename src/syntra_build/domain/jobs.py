"""Domain records and vocabulary for durable asynchronous jobs."""

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
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId


class JobState(StrEnum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"


class WorkerClass(StrEnum):
    ARCHITECT = "ARCHITECT"
    CODEX = "CODEX"
    GIT = "GIT"
    GITHUB = "GITHUB"
    CI = "CI"
    MESSAGING = "MESSAGING"
    RECOVERY = "RECOVERY"
    INTERNAL = "INTERNAL"


class JobAttemptState(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"


StructuredMetadata = Mapping[str, str | int | float | bool | None]


def _metadata(value: StructuredMetadata | None, name: str) -> StructuredMetadata:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, (str, int, float, bool, type(None)))
        for key, item in value.items()
    ):
        raise DomainValidationError(f"{name} must contain JSON scalar values")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class Job:
    id: JobId
    project_id: ProjectId
    job_type: str
    state: JobState
    attempt_number: int
    created_at: datetime
    updated_at: datetime
    milestone_id: MilestoneId | None = None
    priority: int = 0
    correlation_id: str = "uncorrelated"
    max_attempts: int = 1
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    next_retry_at: datetime | None = None
    timeout_seconds: int | None = None
    worker_class: WorkerClass = WorkerClass.INTERNAL
    payload: StructuredMetadata | None = None
    result: StructuredMetadata | None = None
    last_error_id: str | None = None
    failure_classification: FailureClassification | None = None
    retry_exhausted: bool = False
    exhaustion_reason: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, JobId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        if self.milestone_id is not None:
            require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_text(self.job_type, "job_type")
        require_enum(self.state, JobState, "state")
        require_enum(self.worker_class, WorkerClass, "worker_class")
        require_text(self.correlation_id, "correlation_id")
        for value, name, minimum in (
            (self.attempt_number, "attempt_number", 0),
            (self.max_attempts, "max_attempts", 1),
            (self.priority, "priority", 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise DomainValidationError(f"{name} must be an integer >= {minimum}")
        if self.attempt_number > self.max_attempts:
            raise DomainValidationError("attempt_number cannot exceed max_attempts")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool) or self.timeout_seconds < 1
        ):
            raise DomainValidationError("timeout_seconds must be a positive integer")
        require_utc(self.created_at, "created_at")
        require_utc(self.updated_at, "updated_at")
        require_timestamp_order(
            self.created_at, self.updated_at, "created_at", "updated_at"
        )
        for timestamp, name in (
            (self.scheduled_at, "scheduled_at"),
            (self.started_at, "started_at"),
            (self.completed_at, "completed_at"),
            (self.next_retry_at, "next_retry_at"),
        ):
            if timestamp is not None:
                require_utc(timestamp, name)
        if self.scheduled_at is not None:
            require_timestamp_order(
                self.created_at, self.scheduled_at, "created_at", "scheduled_at"
            )
        if self.started_at is not None:
            require_timestamp_order(
                self.created_at, self.started_at, "created_at", "started_at"
            )
        if self.completed_at is not None:
            if self.started_at is not None:
                require_timestamp_order(
                    self.started_at, self.completed_at, "started_at", "completed_at"
                )
            else:
                require_timestamp_order(
                    self.created_at, self.completed_at, "created_at", "completed_at"
                )
        object.__setattr__(self, "payload", _metadata(self.payload, "payload"))
        object.__setattr__(self, "result", _metadata(self.result, "result"))
        if self.failure_classification is not None:
            require_enum(
                self.failure_classification,
                FailureClassification,
                "failure_classification",
            )

    @property
    def attempt_count(self) -> int:
        """Compatibility alias: M9 standardises the counter as attempt_number."""
        return self.attempt_number


@dataclass(frozen=True, slots=True)
class JobAttempt:
    job_id: JobId
    attempt_number: int
    state: JobAttemptState
    started_at: datetime
    completed_at: datetime | None = None
    external_request_id: str | None = None
    process_id: str | None = None
    exit_code: int | None = None
    result: StructuredMetadata | None = None
    error_id: str | None = None
    logs_reference: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.job_id, JobId, "job_id")
        require_enum(self.state, JobAttemptState, "state")
        if isinstance(self.attempt_number, bool) or self.attempt_number < 1:
            raise DomainValidationError("attempt_number must be a positive integer")
        require_utc(self.started_at, "started_at")
        if self.completed_at is not None:
            require_utc(self.completed_at, "completed_at")
            require_timestamp_order(
                self.started_at, self.completed_at, "started_at", "completed_at"
            )
        object.__setattr__(self, "result", _metadata(self.result, "result"))


def consumes_worker_capacity(state: JobState) -> bool:
    """Return whether the state represents active local execution."""
    require_enum(state, JobState, "state")
    return state is JobState.RUNNING
