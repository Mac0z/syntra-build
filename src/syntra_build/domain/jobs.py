"""Durable-work job domain records."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_timestamp_order,
    require_utc,
)
from syntra_build.domain.errors import DomainValidationError
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


@dataclass(frozen=True, slots=True)
class Job:
    id: JobId
    project_id: ProjectId
    job_type: str
    state: JobState
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    milestone_id: MilestoneId | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, JobId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        if self.milestone_id is not None:
            require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_text(self.job_type, "job_type")
        require_enum(self.state, JobState, "state")
        if isinstance(self.attempt_count, bool) or not isinstance(
            self.attempt_count, int
        ):
            raise DomainValidationError("attempt_count must be an integer")
        if self.attempt_count < 0:
            raise DomainValidationError("attempt_count cannot be negative")
        require_utc(self.created_at, "created_at")
        require_utc(self.updated_at, "updated_at")
        require_timestamp_order(
            self.created_at, self.updated_at, "created_at", "updated_at"
        )
