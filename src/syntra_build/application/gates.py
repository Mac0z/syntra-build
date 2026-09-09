"""Trusted orchestration for durable human gates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from syntra_build.application.commands.models import Command, CommandType
from syntra_build.domain.gate_state_machine import GateTransitionRequest
from syntra_build.domain.gates import (
    DesignApprovalResponse,
    ExpectedResponseType,
    GateState,
    GateType,
    HumanGate,
    HumanGateResponse,
    HumanTestResponse,
    validate_response,
)
from syntra_build.domain.identifiers import GateId, MilestoneId, ProjectId
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.projects import ProjectState
from syntra_build.infrastructure.persistence.errors import (
    ClosedGateError,
    DuplicateGateResponseError,
    GateMilestoneProjectMismatchError,
    GateNotFoundError,
    PersistenceError,
)
from syntra_build.infrastructure.persistence.gates import SQLiteHumanGateRepository


class GateNotifier(Protocol):
    def send(self, text: str) -> str: ...


class DesignDecisionHandler(Protocol):
    def respond(self, command: Command, gate: HumanGate) -> str: ...


@dataclass(frozen=True, slots=True)
class CreateGateRequest:
    project_id: ProjectId
    gate_type: GateType
    title: str
    prompt: str
    expected_response_type: ExpectedResponseType
    created_at: datetime
    created_by: str
    correlation_id: str
    milestone_id: MilestoneId | None = None
    options: tuple[str, ...] = ()
    architect_recommendation: str | None = None
    resume_project_state: ProjectState | None = None
    resume_milestone_state: MilestoneState | None = None
    artifact_reference: str | None = None


class HumanGateService:
    def __init__(
        self,
        repository: SQLiteHumanGateRepository,
        *,
        gate_id_factory: Callable[[], GateId] = GateId.generate,
        response_id_factory: Callable[[], str],
        authorised_responder_ids: frozenset[str],
    ):
        self._repository = repository
        self._gate_ids = gate_id_factory
        self._response_ids = response_id_factory
        self._authorised = authorised_responder_ids

    def create(self, request: CreateGateRequest) -> HumanGate:
        required_schema = {
            GateType.DESIGN_APPROVAL: ExpectedResponseType.DESIGN_APPROVAL,
            GateType.HUMAN_TEST: ExpectedResponseType.HUMAN_TEST,
            GateType.PRODUCT_DECISION: ExpectedResponseType.OPTION,
            GateType.TECHNICAL_DECISION: ExpectedResponseType.OPTION,
            GateType.RECOVERY_DECISION: ExpectedResponseType.OPTION,
            GateType.FINAL_ACCEPTANCE: ExpectedResponseType.OPTION,
        }[request.gate_type]
        if request.expected_response_type is not required_schema:
            raise ValueError("response schema does not match gate type")
        if (
            request.expected_response_type is ExpectedResponseType.OPTION
            and not request.options
        ):
            raise ValueError("option gates require at least one option")
        if request.milestone_id and request.resume_project_state:
            raise GateMilestoneProjectMismatchError(
                "milestone gate cannot use project resume target"
            )
        gate = HumanGate(
            id=self._gate_ids(),
            project_id=request.project_id,
            milestone_id=request.milestone_id,
            gate_type=request.gate_type,
            state=GateState.PENDING,
            requested_at=request.created_at,
            title=request.title,
            prompt=request.prompt,
            expected_response_type=request.expected_response_type,
            options=request.options,
            architect_recommendation=request.architect_recommendation,
            resume_project_state=request.resume_project_state,
            resume_milestone_state=request.resume_milestone_state,
            created_by=request.created_by,
            correlation_id=request.correlation_id,
            artifact_reference=request.artifact_reference,
        )
        self._repository.add(gate)
        return gate

    def notify(
        self,
        gate_id: GateId,
        notifier: GateNotifier,
        *,
        occurred_at: datetime,
        actor_id: str | None = None,
    ) -> HumanGate:
        gate = self._repository.get(gate_id)
        options = gate.options
        if not options and gate.gate_type is GateType.DESIGN_APPROVAL:
            options = tuple(x.value for x in DesignApprovalResponse)
        elif not options and gate.gate_type is GateType.HUMAN_TEST:
            options = tuple(x.value for x in HumanTestResponse)
        scope = f"project {gate.project_id}" + (
            f", milestone {gate.milestone_id}" if gate.milestone_id else ""
        )
        artifact = (
            f"\nArtifact: {gate.artifact_reference}" if gate.artifact_reference else ""
        )
        notifier.send(
            f"{gate.title} — {scope}\n\nGate: {gate.id}\n{gate.prompt}{artifact}\n\n"
            + "\n".join(options)
        )
        return self._repository.apply_transition(
            self._transition(
                gate,
                GateState.NOTIFIED,
                "notification confirmed",
                occurred_at,
                "SYSTEM",
                actor_id,
            )
        )

    def respond(
        self,
        gate_id: GateId,
        *,
        project_id: ProjectId,
        milestone_id: MilestoneId | None,
        message_id: str,
        response_code: str,
        response_text: str | None,
        responded_by: str,
        responded_at: datetime,
        correlation_id: str | None = None,
        source_reference: str | None = None,
    ) -> HumanGate:
        if responded_by not in self._authorised:
            raise PermissionError("responder is not authorised")
        gate = self._repository.get(gate_id)
        if gate.project_id != project_id:
            raise GateMilestoneProjectMismatchError("gate belongs to another project")
        if gate.milestone_id != milestone_id:
            raise GateMilestoneProjectMismatchError("gate belongs to another milestone")
        if gate.state is not GateState.NOTIFIED:
            raise ClosedGateError("gate is not answerable")
        if self._repository.resolution_would_orphan_waiting_project(gate):
            raise PersistenceError(
                "resolving the final gate requires an atomic project resume"
            )
        code = validate_response(gate, response_code)
        response = HumanGateResponse(
            self._response_ids(),
            gate.id,
            message_id,
            code,
            response_text,
            code
            if gate.expected_response_type is ExpectedResponseType.OPTION
            else None,
            (),
            responded_by,
            responded_at,
            True,
            "schema validated",
        )
        gate = self._repository.record_response(
            self._transition(
                gate,
                GateState.RESPONDED,
                "human response received",
                responded_at,
                "HUMAN",
                responded_by,
                source_reference or message_id,
                correlation_id,
            ),
            response,
        )
        gate = self._repository.apply_transition(
            self._transition(
                gate,
                GateState.VALIDATED,
                "response schema validated",
                responded_at,
                "SYSTEM",
                None,
                source_reference or message_id,
                correlation_id,
            )
        )
        return self._repository.apply_transition(
            self._transition(
                gate,
                GateState.RESOLVED,
                "validated response resolved gate",
                responded_at,
                "SYSTEM",
                None,
                source_reference or message_id,
                correlation_id,
            )
        )

    def _transition(
        self,
        gate: HumanGate,
        target: GateState,
        reason: str,
        at: datetime,
        actor_type: str,
        actor_id: str | None,
        trigger: str | None = None,
        correlation_id: str | None = None,
    ) -> GateTransitionRequest:
        return GateTransitionRequest(
            gate.id,
            gate.project_id,
            gate.state,
            target,
            reason,
            actor_type,
            actor_id,
            correlation_id or gate.correlation_id,
            at,
            gate.milestone_id,
            trigger,
        )

    def outstanding(self, project_id: ProjectId | None = None) -> tuple[HumanGate, ...]:
        return self._repository.outstanding(project_id)

    def get(self, gate_id: GateId) -> HumanGate:
        """Return one authoritative persisted gate for trusted correlation."""
        return self._repository.get(gate_id)


class HumanGateCommandHandler:
    """Provider-neutral bridge from deterministic M6 commands to gate services."""

    def __init__(
        self,
        gates: HumanGateService,
        design_decisions: DesignDecisionHandler | None = None,
    ):
        self._gates = gates
        self._design_decisions = design_decisions

    def waiting(self) -> str:
        return format_waiting(self._gates.outstanding())

    def respond(self, command: Command) -> str:
        if command.type is not CommandType.RESPOND_GATE:
            return "Invalid human gate command."
        if command.gate_reference is None or command.gate_response is None:
            return "Invalid gate response syntax."
        try:
            gate_id = GateId.from_string(command.gate_reference)
            gate = self._gates.get(gate_id)
            if gate.gate_type is GateType.DESIGN_APPROVAL and self._design_decisions:
                if gate.state is not GateState.NOTIFIED:
                    raise ClosedGateError("gate is not answerable")
                return self._design_decisions.respond(command, gate)
            resolved = self._gates.respond(
                gate_id,
                project_id=gate.project_id,
                milestone_id=gate.milestone_id,
                message_id=command.source_message_id,
                response_code=command.gate_response,
                response_text=command.gate_feedback,
                responded_by=command.requested_by,
                responded_at=command.requested_at,
                correlation_id=command.correlation_id,
                source_reference=(
                    f"{command.source_platform}:{command.source_update_id}:"
                    f"{command.source_message_id}"
                ),
            )
        except GateNotFoundError:
            return "Human gate not found."
        except ClosedGateError:
            return "Human gate is already closed and cannot be answered."
        except DuplicateGateResponseError:
            return "This response message was already processed."
        except PermissionError:
            return "You are not authorised to answer this human gate."
        except ValueError, PersistenceError:
            return "Human gate response was rejected."
        return f"Human gate {resolved.id} resolved as {command.gate_response}."


def format_waiting(gates: tuple[HumanGate, ...]) -> str:
    if not gates:
        return "No human actions are currently waiting."
    lines = [
        f"{len(gates)} action{'s' if len(gates) != 1 else ''} need your attention:",
        "",
    ]
    for number, gate in enumerate(gates, 1):
        milestone = f" M{gate.milestone_id}" if gate.milestone_id else ""
        lines.extend(
            (
                f"{number}. Project {gate.project_id}{milestone} — {gate.title}",
                f"   Gate: {gate.id}",
                f"   {gate.prompt}",
            )
        )
    return "\n".join(lines)
