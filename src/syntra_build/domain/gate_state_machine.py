"""Deterministic, provider-independent human-gate lifecycle policy."""

from dataclasses import dataclass
from datetime import datetime

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import InvalidGateTransitionError
from syntra_build.domain.gates import GateState
from syntra_build.domain.identifiers import GateId, MilestoneId, ProjectId

VALID_GATE_TRANSITIONS = {
    GateState.PENDING: frozenset({GateState.NOTIFIED, GateState.CANCELLED}),
    GateState.NOTIFIED: frozenset(
        {GateState.RESPONDED, GateState.EXPIRED, GateState.CANCELLED}
    ),
    GateState.RESPONDED: frozenset(
        {GateState.VALIDATED, GateState.NOTIFIED, GateState.CANCELLED}
    ),
    GateState.VALIDATED: frozenset({GateState.RESOLVED}),
    GateState.RESOLVED: frozenset(),
    GateState.EXPIRED: frozenset(),
    GateState.CANCELLED: frozenset(),
}


def is_gate_transition_allowed(previous: GateState, target: GateState) -> bool:
    return target in VALID_GATE_TRANSITIONS[previous]


def validate_gate_transition(previous: GateState, target: GateState) -> None:
    if not is_gate_transition_allowed(previous, target):
        raise InvalidGateTransitionError(
            f"human gate cannot transition from {previous} to {target}"
        )


@dataclass(frozen=True, slots=True)
class GateTransitionRequest:
    gate_id: GateId
    project_id: ProjectId
    expected_state: GateState
    target_state: GateState
    reason: str
    actor_type: str
    actor_id: str | None
    correlation_id: str
    occurred_at: datetime
    milestone_id: MilestoneId | None = None
    trigger_event_id: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.gate_id, GateId, "gate_id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_enum(self.expected_state, GateState, "expected_state")
        require_enum(self.target_state, GateState, "target_state")
        require_text(self.reason, "reason")
        require_text(self.actor_type, "actor_type")
        require_text(self.correlation_id, "correlation_id")
        require_utc(self.occurred_at, "occurred_at")
