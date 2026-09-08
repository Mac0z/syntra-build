"""Deterministic, provider-independent project lifecycle policy."""

from dataclasses import dataclass
from datetime import datetime

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import InvalidProjectTransitionError
from syntra_build.domain.identifiers import ProjectId
from syntra_build.domain.projects import ProjectState

VALID_PROJECT_TRANSITIONS: dict[ProjectState, frozenset[ProjectState]] = {
    ProjectState.NEW: frozenset(
        {ProjectState.DESIGNING, ProjectState.CANCELLED, ProjectState.FAILED}
    ),
    ProjectState.DESIGNING: frozenset(
        {
            ProjectState.DESIGN_APPROVAL,
            ProjectState.PAUSED,
            ProjectState.BLOCKED,
            ProjectState.CANCELLED,
            ProjectState.FAILED,
        }
    ),
    ProjectState.DESIGN_APPROVAL: frozenset(
        {
            ProjectState.DESIGNING,
            ProjectState.PROVISIONING,
            ProjectState.PAUSED,
            ProjectState.CANCELLED,
            ProjectState.FAILED,
        }
    ),
    ProjectState.PROVISIONING: frozenset(
        {
            ProjectState.READY,
            ProjectState.BLOCKED,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        }
    ),
    ProjectState.READY: frozenset(
        {
            ProjectState.BUILDING,
            ProjectState.PAUSED,
            ProjectState.BLOCKED,
            ProjectState.COMPLETING,
            ProjectState.CANCELLED,
            ProjectState.FAILED,
        }
    ),
    ProjectState.BUILDING: frozenset(
        {
            ProjectState.WAITING_HUMAN,
            ProjectState.READY,
            ProjectState.PAUSED,
            ProjectState.BLOCKED,
            ProjectState.COMPLETING,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        }
    ),
    ProjectState.WAITING_HUMAN: frozenset(
        {
            ProjectState.DESIGNING,
            ProjectState.BUILDING,
            ProjectState.COMPLETING,
            ProjectState.PAUSED,
            ProjectState.BLOCKED,
            ProjectState.CANCELLED,
            ProjectState.FAILED,
        }
    ),
    ProjectState.PAUSED: frozenset(
        {
            ProjectState.DESIGNING,
            ProjectState.DESIGN_APPROVAL,
            ProjectState.READY,
            ProjectState.BUILDING,
            ProjectState.WAITING_HUMAN,
            ProjectState.BLOCKED,
            ProjectState.CANCELLED,
        }
    ),
    ProjectState.BLOCKED: frozenset(
        {
            ProjectState.DESIGNING,
            ProjectState.READY,
            ProjectState.BUILDING,
            ProjectState.WAITING_HUMAN,
            ProjectState.PAUSED,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        }
    ),
    ProjectState.COMPLETING: frozenset(
        {
            ProjectState.WAITING_HUMAN,
            ProjectState.COMPLETE,
            ProjectState.BLOCKED,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        }
    ),
    ProjectState.COMPLETE: frozenset(),
    ProjectState.FAILED: frozenset(),
    ProjectState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ProjectTransitionRequest:
    project_id: ProjectId
    expected_state: ProjectState
    target_state: ProjectState
    reason: str
    actor_type: str
    actor_id: str | None
    correlation_id: str
    occurred_at: datetime
    trigger_event_id: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.project_id, ProjectId, "project_id")
        require_enum(self.expected_state, ProjectState, "expected_state")
        require_enum(self.target_state, ProjectState, "target_state")
        require_text(self.reason, "reason")
        require_text(self.actor_type, "actor_type")
        if self.actor_id is not None:
            require_text(self.actor_id, "actor_id")
        require_text(self.correlation_id, "correlation_id")
        require_utc(self.occurred_at, "occurred_at")
        if self.trigger_event_id is not None:
            require_text(self.trigger_event_id, "trigger_event_id")


def is_transition_allowed(previous: ProjectState, target: ProjectState) -> bool:
    """Return whether the explicit project transition table permits a move."""
    return target in VALID_PROJECT_TRANSITIONS[previous]


def validate_transition(
    previous: ProjectState,
    target: ProjectState,
    resume_state: ProjectState | None = None,
) -> None:
    """Reject illegal moves and arbitrary destinations from PAUSED."""
    if not is_transition_allowed(previous, target):
        raise InvalidProjectTransitionError(
            f"project cannot transition from {previous} to {target}"
        )
    if previous is ProjectState.PAUSED and target not in {
        ProjectState.BLOCKED,
        ProjectState.CANCELLED,
    }:
        if resume_state is None or resume_state is ProjectState.PAUSED:
            raise InvalidProjectTransitionError(
                "paused project has no valid resume state"
            )
        if target is not resume_state:
            raise InvalidProjectTransitionError(
                "paused project must resume to its persisted resume state"
            )
