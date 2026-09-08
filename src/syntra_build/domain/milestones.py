"""Milestone domain records and approved lifecycle vocabulary."""

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
from syntra_build.domain.identifiers import MilestoneId, ProjectId


class MilestoneState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    PREPARING_TASK = "PREPARING_TASK"
    PREPARING_WORKSPACE = "PREPARING_WORKSPACE"
    CODING = "CODING"
    VALIDATING_CHANGES = "VALIDATING_CHANGES"
    COMMITTING = "COMMITTING"
    PUSHING = "PUSHING"
    PR_CREATING = "PR_CREATING"
    CI_RUNNING = "CI_RUNNING"
    CI_REWORK = "CI_REWORK"
    ARCHITECT_REVIEW = "ARCHITECT_REVIEW"
    REVIEW_REWORK = "REVIEW_REWORK"
    HUMAN_DECISION = "HUMAN_DECISION"
    HUMAN_TEST = "HUMAN_TEST"
    MERGE_READY = "MERGE_READY"
    MERGING = "MERGING"
    MERGE_VERIFY = "MERGE_VERIFY"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class Milestone:
    id: MilestoneId
    project_id: ProjectId
    sequence_number: int
    code: str
    title: str
    state: MilestoneState
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        require_identifier(self.id, MilestoneId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        if isinstance(self.sequence_number, bool) or not isinstance(
            self.sequence_number, int
        ):
            raise DomainValidationError("sequence_number must be an integer")
        if self.sequence_number < 0:
            raise DomainValidationError("sequence_number cannot be negative")
        require_text(self.code, "code")
        require_text(self.title, "title")
        require_enum(self.state, MilestoneState, "state")
        require_utc(self.created_at, "created_at")
        require_utc(self.updated_at, "updated_at")
        require_timestamp_order(
            self.created_at, self.updated_at, "created_at", "updated_at"
        )
