"""Deterministic, provider-independent milestone lifecycle policy."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import (
    InvalidBlockedRecoveryError,
    InvalidMilestoneTransitionError,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.milestones import MilestoneState


def _states(*states: MilestoneState) -> frozenset[MilestoneState]:
    return frozenset(states)


VALID_MILESTONE_TRANSITIONS: dict[MilestoneState, frozenset[MilestoneState]] = {
    MilestoneState.PENDING: _states(MilestoneState.READY, MilestoneState.CANCELLED),
    MilestoneState.READY: _states(
        MilestoneState.PREPARING_TASK, MilestoneState.BLOCKED, MilestoneState.CANCELLED
    ),
    MilestoneState.PREPARING_TASK: _states(
        MilestoneState.PREPARING_WORKSPACE,
        MilestoneState.HUMAN_DECISION,
        MilestoneState.BLOCKED,
        MilestoneState.FAILED,
    ),
    MilestoneState.PREPARING_WORKSPACE: _states(
        MilestoneState.CODING, MilestoneState.BLOCKED, MilestoneState.FAILED
    ),
    MilestoneState.CODING: _states(
        MilestoneState.VALIDATING_CHANGES, MilestoneState.BLOCKED, MilestoneState.FAILED
    ),
    MilestoneState.VALIDATING_CHANGES: _states(
        MilestoneState.COMMITTING,
        MilestoneState.CODING,
        MilestoneState.HUMAN_DECISION,
        MilestoneState.BLOCKED,
        MilestoneState.FAILED,
    ),
    MilestoneState.COMMITTING: _states(
        MilestoneState.PUSHING, MilestoneState.BLOCKED, MilestoneState.FAILED
    ),
    MilestoneState.PUSHING: _states(
        MilestoneState.PR_CREATING,
        MilestoneState.CI_RUNNING,
        MilestoneState.BLOCKED,
        MilestoneState.FAILED,
    ),
    MilestoneState.PR_CREATING: _states(
        MilestoneState.CI_RUNNING, MilestoneState.BLOCKED, MilestoneState.FAILED
    ),
    MilestoneState.CI_RUNNING: _states(
        MilestoneState.ARCHITECT_REVIEW,
        MilestoneState.CI_REWORK,
        MilestoneState.BLOCKED,
        MilestoneState.FAILED,
    ),
    MilestoneState.CI_REWORK: _states(
        MilestoneState.CODING,
        MilestoneState.HUMAN_DECISION,
        MilestoneState.BLOCKED,
        MilestoneState.FAILED,
    ),
    MilestoneState.ARCHITECT_REVIEW: _states(
        MilestoneState.MERGE_READY,
        MilestoneState.REVIEW_REWORK,
        MilestoneState.HUMAN_TEST,
        MilestoneState.HUMAN_DECISION,
        MilestoneState.BLOCKED,
        MilestoneState.FAILED,
    ),
    MilestoneState.REVIEW_REWORK: _states(
        MilestoneState.CODING,
        MilestoneState.HUMAN_DECISION,
        MilestoneState.BLOCKED,
        MilestoneState.FAILED,
    ),
    MilestoneState.HUMAN_DECISION: _states(
        MilestoneState.PREPARING_TASK,
        MilestoneState.CODING,
        MilestoneState.ARCHITECT_REVIEW,
        MilestoneState.MERGE_READY,
        MilestoneState.BLOCKED,
        MilestoneState.CANCELLED,
    ),
    MilestoneState.HUMAN_TEST: _states(
        MilestoneState.MERGE_READY,
        MilestoneState.REVIEW_REWORK,
        MilestoneState.HUMAN_DECISION,
        MilestoneState.BLOCKED,
        MilestoneState.CANCELLED,
    ),
    MilestoneState.MERGE_READY: _states(
        MilestoneState.MERGING,
        MilestoneState.CI_RUNNING,
        MilestoneState.ARCHITECT_REVIEW,
        MilestoneState.HUMAN_TEST,
        MilestoneState.HUMAN_DECISION,
        MilestoneState.BLOCKED,
        MilestoneState.FAILED,
    ),
    MilestoneState.MERGING: _states(
        MilestoneState.MERGE_VERIFY, MilestoneState.BLOCKED, MilestoneState.FAILED
    ),
    MilestoneState.MERGE_VERIFY: _states(
        MilestoneState.COMPLETE, MilestoneState.BLOCKED, MilestoneState.FAILED
    ),
    MilestoneState.COMPLETE: frozenset(),
    MilestoneState.BLOCKED: _states(MilestoneState.FAILED, MilestoneState.CANCELLED),
    MilestoneState.FAILED: frozenset(),
    MilestoneState.CANCELLED: frozenset(),
}

ACTIVE_MILESTONE_STATES = frozenset(MilestoneState) - {
    MilestoneState.PENDING,
    MilestoneState.COMPLETE,
    MilestoneState.FAILED,
    MilestoneState.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class MilestoneTransitionRequest:
    milestone_id: MilestoneId
    project_id: ProjectId
    expected_state: MilestoneState
    target_state: MilestoneState
    reason: str
    actor_type: str
    actor_id: str | None
    correlation_id: str
    occurred_at: datetime
    trigger_event_id: str | None = None
    metadata: Mapping[str, str | int | bool | None] | None = None

    def __post_init__(self) -> None:
        require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_enum(self.expected_state, MilestoneState, "expected_state")
        require_enum(self.target_state, MilestoneState, "target_state")
        require_text(self.reason, "reason")
        require_text(self.actor_type, "actor_type")
        if self.actor_id is not None:
            require_text(self.actor_id, "actor_id")
        require_text(self.correlation_id, "correlation_id")
        require_utc(self.occurred_at, "occurred_at")
        if self.trigger_event_id is not None:
            require_text(self.trigger_event_id, "trigger_event_id")
        if self.metadata is not None:
            if not isinstance(self.metadata, Mapping) or not all(
                isinstance(key, str) and isinstance(value, (str, int, bool, type(None)))
                for key, value in self.metadata.items()
            ):
                raise InvalidMilestoneTransitionError(
                    "transition metadata must use string keys and scalar values"
                )


def is_milestone_transition_allowed(
    previous: MilestoneState,
    target: MilestoneState,
    resume_state: MilestoneState | None = None,
) -> bool:
    if previous is MilestoneState.BLOCKED:
        return target in {MilestoneState.FAILED, MilestoneState.CANCELLED} or (
            resume_state is not None and target is resume_state
        )
    return target in VALID_MILESTONE_TRANSITIONS[previous]


def validate_milestone_transition(
    previous: MilestoneState,
    target: MilestoneState,
    resume_state: MilestoneState | None = None,
) -> None:
    if previous is MilestoneState.BLOCKED and target not in {
        MilestoneState.FAILED,
        MilestoneState.CANCELLED,
    }:
        if resume_state is None or resume_state is MilestoneState.BLOCKED:
            raise InvalidBlockedRecoveryError(
                "blocked milestone has no valid recovery target"
            )
        if target is not resume_state:
            raise InvalidBlockedRecoveryError(
                "blocked milestone must recover to its persisted recovery target"
            )
        return
    if not is_milestone_transition_allowed(previous, target, resume_state):
        raise InvalidMilestoneTransitionError(
            f"milestone cannot transition from {previous} to {target}"
        )
