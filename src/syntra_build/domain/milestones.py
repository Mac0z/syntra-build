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
    resume_state: MilestoneState | None = None
    activity: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    codex_cycle_count: int = 0
    ci_rework_count: int = 0
    architect_rework_count: int = 0
    human_test_rework_count: int = 0
    exhaustion_reason: str | None = None

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
        if self.resume_state is not None:
            require_enum(self.resume_state, MilestoneState, "resume_state")
            if self.resume_state is MilestoneState.BLOCKED:
                raise DomainValidationError("resume_state cannot be BLOCKED")
        if self.activity is not None and not isinstance(self.activity, str):
            raise DomainValidationError("activity must be a string or None")
        for name, value in (
            ("started_at", self.started_at),
            ("completed_at", self.completed_at),
        ):
            if value is not None:
                require_utc(value, name)
                require_timestamp_order(self.created_at, value, "created_at", name)
        for name in (
            "codex_cycle_count",
            "ci_rework_count",
            "architect_rework_count",
            "human_test_rework_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise DomainValidationError(f"{name} must be a non-negative integer")
