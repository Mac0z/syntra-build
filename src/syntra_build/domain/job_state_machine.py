"""Deterministic, provider-independent job lifecycle policy."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import InvalidJobTransitionError
from syntra_build.domain.identifiers import JobId, ProjectId
from syntra_build.domain.jobs import JobState, StructuredMetadata

VALID_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.DISPATCHED, JobState.CANCELLED}),
    JobState.DISPATCHED: frozenset(
        {JobState.RUNNING, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING_EXTERNAL,
            JobState.SUCCEEDED,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.WAITING_EXTERNAL: frozenset(
        {JobState.SUCCEEDED, JobState.RETRY_WAIT, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.RETRY_WAIT: frozenset(
        {JobState.QUEUED, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.ABANDONED: frozenset(),
}
TERMINAL_JOB_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.ABANDONED}
)


@dataclass(frozen=True, slots=True)
class JobTransitionRequest:
    job_id: JobId
    project_id: ProjectId
    expected_state: JobState
    target_state: JobState
    reason: str
    actor_type: str
    actor_id: str | None
    correlation_id: str
    occurred_at: datetime
    trigger_event_id: str | None = None
    metadata: Mapping[str, str | int | bool | None] | None = None
    next_retry_at: datetime | None = None
    result: StructuredMetadata | None = None
    error_id: str | None = None
    exit_code: int | None = None
    external_request_id: str | None = None
    process_id: str | None = None
    logs_reference: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.job_id, JobId, "job_id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_enum(self.expected_state, JobState, "expected_state")
        require_enum(self.target_state, JobState, "target_state")
        require_text(self.reason, "reason")
        require_text(self.actor_type, "actor_type")
        require_text(self.correlation_id, "correlation_id")
        require_utc(self.occurred_at, "occurred_at")
        if self.next_retry_at is not None:
            require_utc(self.next_retry_at, "next_retry_at")


def validate_job_transition(previous: JobState, target: JobState) -> None:
    if target not in VALID_JOB_TRANSITIONS[previous]:
        raise InvalidJobTransitionError(
            f"job cannot transition from {previous} to {target}"
        )
