import pytest

from syntra_build.domain import GateState
from syntra_build.domain.errors import InvalidGateTransitionError
from syntra_build.domain.gate_state_machine import (
    VALID_GATE_TRANSITIONS,
    validate_gate_transition,
)

VALID = [
    (old, new) for old, targets in VALID_GATE_TRANSITIONS.items() for new in targets
]


@pytest.mark.parametrize(("old", "new"), VALID)
def test_all_documented_transitions(old: GateState, new: GateState) -> None:
    validate_gate_transition(old, new)


@pytest.mark.parametrize("state", list(GateState))
def test_same_state_is_rejected(state: GateState) -> None:
    with pytest.raises(InvalidGateTransitionError):
        validate_gate_transition(state, state)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (GateState.PENDING, GateState.RESPONDED),
        (GateState.PENDING, GateState.RESOLVED),
        (GateState.NOTIFIED, GateState.VALIDATED),
        (GateState.RESPONDED, GateState.RESOLVED),
        (GateState.VALIDATED, GateState.NOTIFIED),
        (GateState.RESOLVED, GateState.RESPONDED),
        (GateState.CANCELLED, GateState.NOTIFIED),
    ],
)
def test_undocumented_transitions_are_rejected(old: GateState, new: GateState) -> None:
    with pytest.raises(InvalidGateTransitionError):
        validate_gate_transition(old, new)
