from datetime import UTC, datetime

import pytest

from syntra_build.domain import (
    VALID_MILESTONE_TRANSITIONS,
    InvalidBlockedRecoveryError,
    InvalidMilestoneTransitionError,
    MilestoneId,
    MilestoneState,
    MilestoneTransitionRequest,
    ProjectId,
    is_milestone_transition_allowed,
    validate_milestone_transition,
)

EXPLICIT_TRANSITIONS = [
    (source, target)
    for source, targets in VALID_MILESTONE_TRANSITIONS.items()
    if source is not MilestoneState.BLOCKED
    for target in targets
]


@pytest.mark.parametrize(("source", "target"), EXPLICIT_TRANSITIONS)
def test_every_documented_transition_is_allowed(
    source: MilestoneState, target: MilestoneState
) -> None:
    assert is_milestone_transition_allowed(source, target)
    validate_milestone_transition(source, target)


@pytest.mark.parametrize("state", list(MilestoneState))
def test_same_state_transition_is_rejected(state: MilestoneState) -> None:
    with pytest.raises(InvalidMilestoneTransitionError):
        validate_milestone_transition(state, state)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (MilestoneState.PENDING, MilestoneState.CODING),
        (MilestoneState.READY, MilestoneState.CI_RUNNING),
        (MilestoneState.CODING, MilestoneState.MERGE_READY),
        (MilestoneState.CI_RUNNING, MilestoneState.COMPLETE),
        (MilestoneState.ARCHITECT_REVIEW, MilestoneState.COMPLETE),
        (MilestoneState.MERGING, MilestoneState.COMPLETE),
        (MilestoneState.COMPLETE, MilestoneState.CODING),
        (MilestoneState.FAILED, MilestoneState.READY),
        (MilestoneState.CANCELLED, MilestoneState.PENDING),
    ],
)
def test_representative_illegal_transitions(
    source: MilestoneState, target: MilestoneState
) -> None:
    with pytest.raises(InvalidMilestoneTransitionError):
        validate_milestone_transition(source, target)


def test_blocked_recovery_is_exact_and_escalations_are_explicit() -> None:
    validate_milestone_transition(
        MilestoneState.BLOCKED, MilestoneState.CODING, MilestoneState.CODING
    )
    for target in (MilestoneState.FAILED, MilestoneState.CANCELLED):
        validate_milestone_transition(
            MilestoneState.BLOCKED, target, MilestoneState.CODING
        )
    with pytest.raises(InvalidBlockedRecoveryError):
        validate_milestone_transition(
            MilestoneState.BLOCKED, MilestoneState.CI_RUNNING, MilestoneState.CODING
        )


def test_transition_request_validates_typed_metadata() -> None:
    request = MilestoneTransitionRequest(
        MilestoneId.generate(),
        ProjectId.generate(),
        MilestoneState.PENDING,
        MilestoneState.READY,
        "dependencies complete",
        "SYSTEM",
        None,
        "correlation",
        datetime(2026, 9, 8, tzinfo=UTC),
        "event",
    )
    assert request.target_state is MilestoneState.READY
