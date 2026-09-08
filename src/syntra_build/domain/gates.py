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
from syntra_build.domain.errors import InvalidGateResponseError
from syntra_build.domain.identifiers import GateId, MilestoneId, ProjectId
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.projects import ProjectState


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


class ExpectedResponseType(StrEnum):
    DESIGN_APPROVAL = "DESIGN_APPROVAL"
    HUMAN_TEST = "HUMAN_TEST"
    OPTION = "OPTION"


class DesignApprovalResponse(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    CANCEL_PROJECT = "CANCEL_PROJECT"


class HumanTestResponse(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class HumanGate:
    id: GateId
    project_id: ProjectId
    gate_type: GateType
    state: GateState
    requested_at: datetime
    title: str = "Human action required"
    prompt: str = "Respond using an allowed option."
    expected_response_type: ExpectedResponseType = ExpectedResponseType.OPTION
    options: tuple[str, ...] = ()
    milestone_id: MilestoneId | None = None
    architect_recommendation: str | None = None
    resume_project_state: ProjectState | None = None
    resume_milestone_state: MilestoneState | None = None
    notified_at: datetime | None = None
    responded_at: datetime | None = None
    resolved_at: datetime | None = None
    created_by: str = "SYSTEM"
    correlation_id: str = "uncorrelated"
    artifact_reference: str | None = None

    @property
    def created_at(self) -> datetime:
        """Approved M10 name, backed by the compatible requested timestamp."""
        return self.requested_at

    def __post_init__(self) -> None:
        require_identifier(self.id, GateId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        if self.milestone_id is not None:
            require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_enum(self.gate_type, GateType, "gate_type")
        require_enum(self.state, GateState, "state")
        require_utc(self.requested_at, "requested_at")
        for name in ("title", "prompt", "created_by", "correlation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidGateResponseError(f"{name} must be non-empty")
        require_enum(
            self.expected_response_type, ExpectedResponseType, "expected_response_type"
        )
        if len(set(self.options)) != len(self.options) or any(
            not option.strip() for option in self.options
        ):
            raise InvalidGateResponseError("options must be unique non-empty values")
        if self.notified_at is not None:
            require_utc(self.notified_at, "notified_at")
            require_timestamp_order(
                self.requested_at, self.notified_at, "requested_at", "notified_at"
            )
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


@dataclass(frozen=True, slots=True)
class HumanGateResponse:
    id: str
    gate_id: GateId
    message_id: str
    response_code: str
    response_text: str | None
    selected_option: str | None
    attachments: tuple[str, ...]
    responded_by: str
    responded_at: datetime
    validated: bool = False
    validation_notes: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.gate_id, GateId, "gate_id")
        for name in ("id", "message_id", "response_code", "responded_by"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidGateResponseError(f"{name} must be non-empty")
        require_utc(self.responded_at, "responded_at")


def validate_response(gate: HumanGate, response_code: str) -> str:
    """Normalise and validate a response against the persisted schema."""
    code = response_code.strip().upper()
    if gate.expected_response_type is ExpectedResponseType.DESIGN_APPROVAL:
        allowed = {item.value for item in DesignApprovalResponse}
    elif gate.expected_response_type is ExpectedResponseType.HUMAN_TEST:
        allowed = {item.value for item in HumanTestResponse}
    else:
        allowed = set(gate.options)
    if code not in allowed:
        raise InvalidGateResponseError("response is not an allowed gate outcome")
    return code
