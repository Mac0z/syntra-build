"""Auditable coverage of the M7 project transition policy."""

from datetime import UTC, datetime

import pytest

from syntra_build.domain import (
    VALID_PROJECT_TRANSITIONS,
    InvalidProjectTransitionError,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    is_transition_allowed,
    validate_transition,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("previous", "target"),
    [
        (previous, target)
        for previous, targets in VALID_PROJECT_TRANSITIONS.items()
        for target in targets
    ],
)
def test_every_documented_transition_is_allowed(
    previous: ProjectState, target: ProjectState
) -> None:
    assert is_transition_allowed(previous, target)
    if previous is not ProjectState.PAUSED:
        validate_transition(previous, target)


@pytest.mark.parametrize(
    ("previous", "target"),
    [
        (ProjectState.NEW, ProjectState.COMPLETE),
        (ProjectState.DESIGNING, ProjectState.BUILDING),
        (ProjectState.PROVISIONING, ProjectState.COMPLETE),
        (ProjectState.BUILDING, ProjectState.NEW),
        (ProjectState.COMPLETE, ProjectState.BUILDING),
        (ProjectState.CANCELLED, ProjectState.DESIGNING),
        (ProjectState.FAILED, ProjectState.BUILDING),
    ],
)
def test_representative_illegal_transitions_are_rejected(
    previous: ProjectState, target: ProjectState
) -> None:
    with pytest.raises(InvalidProjectTransitionError):
        validate_transition(previous, target)


@pytest.mark.parametrize("state", list(ProjectState))
def test_same_state_transitions_are_rejected(state: ProjectState) -> None:
    with pytest.raises(InvalidProjectTransitionError):
        validate_transition(state, state)


def test_paused_resume_requires_the_persisted_target() -> None:
    validate_transition(
        ProjectState.PAUSED, ProjectState.BUILDING, ProjectState.BUILDING
    )
    with pytest.raises(InvalidProjectTransitionError, match="persisted"):
        validate_transition(
            ProjectState.PAUSED, ProjectState.READY, ProjectState.BUILDING
        )
    with pytest.raises(InvalidProjectTransitionError, match="no valid"):
        validate_transition(ProjectState.PAUSED, ProjectState.BUILDING)


def test_transition_request_validates_provenance_and_utc() -> None:
    request = ProjectTransitionRequest(
        ProjectId.generate(),
        ProjectState.NEW,
        ProjectState.DESIGNING,
        "design started",
        "SYSTEM",
        None,
        "correlation",
        NOW,
    )
    assert request.occurred_at is NOW
    with pytest.raises(ValueError):
        ProjectTransitionRequest(
            request.project_id,
            request.expected_state,
            request.target_state,
            "",
            request.actor_type,
            None,
            request.correlation_id,
            NOW,
        )
