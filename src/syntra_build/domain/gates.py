"""Human-gate domain records and approved vocabularies."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_timestamp_order,
    require_utc,
)
from syntra_build.domain.identifiers import GateId, MilestoneId, ProjectId


class GateType(StrEnum):
    DESIGN_APPROVAL = "DESIGN_APPROVAL"
    PRODUCT_DECISION = "PRODUCT_DECISION"
    TECHNICAL_DECISION = "TECHNICAL_DECISION"
    HUMAN_TEST = "HUMAN_TEST"
    FINAL_ACCEPTANCE = "FINAL_ACCEPTANCE"
    RECOVERY_DECISION = "RECOVERY_DECISION"


class GateState(StrEnum):
    PENDING = "PENDING"
    NOTIFIED = "NOTIFIED"
    RESPONDED = "RESPONDED"
    VALIDATED = "VALIDATED"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class HumanGate:
    id: GateId
    project_id: ProjectId
    gate_type: GateType
    state: GateState
    requested_at: datetime
    milestone_id: MilestoneId | None = None
    responded_at: datetime | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, GateId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        if self.milestone_id is not None:
            require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_enum(self.gate_type, GateType, "gate_type")
        require_enum(self.state, GateState, "state")
        require_utc(self.requested_at, "requested_at")
        if self.responded_at is not None:
            require_utc(self.responded_at, "responded_at")
            require_timestamp_order(
                self.requested_at,
                self.responded_at,
                "requested_at",
                "responded_at",
            )
        if self.resolved_at is not None:
            require_utc(self.resolved_at, "resolved_at")
            require_timestamp_order(
                self.requested_at, self.resolved_at, "requested_at", "resolved_at"
            )
            if self.responded_at is not None:
                require_timestamp_order(
                    self.responded_at,
                    self.resolved_at,
                    "responded_at",
                    "resolved_at",
                )
