# ruff: noqa: E501
"""M17 formalisation and durable design-package orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import uuid4

from syntra_build.application.architect import ArchitectError, ArchitectFailureKind
from syntra_build.application.commands.models import Command
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.domain import (
    SPECIFICATION_DRAFT_INTERFACE_VERSION,
    DecisionSource,
    DesignPackage,
    DesignPackageId,
    DocumentType,
    ExpectedResponseType,
    GateId,
    GateState,
    GateType,
    HumanGate,
    ProjectDesignContext,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    RepositoryVisibility,
    SpecificationDraft,
    SpecificationDraftRequest,
)
from syntra_build.infrastructure.persistence import (
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.design_packages import (
    SQLiteDesignPackageRepository,
)
from syntra_build.infrastructure.persistence.gates import SQLiteHumanGateRepository


class SpecificationProvider(Protocol):
    provider_name: str
    model: str

    def draft_specification(
        self, request: SpecificationDraftRequest
    ) -> SpecificationDraft: ...
    def telemetry(self) -> dict[str, int | str | None]: ...


class SpecificationAuditStore(Protocol):
    def begin_draft(
        self,
        *,
        request_id: str,
        request: SpecificationDraftRequest,
        provider: str,
        model: str,
        reasoning_effort: str,
        created_at: datetime,
    ) -> None: ...
    def succeed_draft(
        self,
        *,
        request_id: str,
        response: SpecificationDraft,
        provider_response_id: str | None,
        usage: dict[str, int | str | None],
        completed_at: datetime,
    ) -> None: ...
    def fail(
        self, *, request_id: str, kind: ArchitectFailureKind, completed_at: datetime
    ) -> None: ...


def required_visibility(context: ProjectDesignContext) -> RepositoryVisibility:
    """Private requires an active, explicit human decision; policy defaults public."""
    for decision in reversed(context.decisions):
        if (
            decision.source is DecisionSource.HUMAN
            and decision.decision_type.casefold()
            in {"repository_visibility", "repository visibility"}
            and decision.decision.strip().casefold() == "private"
            and decision.superseded_by_decision_id is None
        ):
            return RepositoryVisibility.PRIVATE
    return RepositoryVisibility.PUBLIC


def build_specification_request(
    context: ProjectDesignContext, correlation_id: str, feedback: tuple[str, ...] = ()
) -> SpecificationDraftRequest:
    messages = cast(
        tuple[dict[str, object], ...],
        tuple(
            {
                "direction": item.direction.value,
                "text": item.text,
                "occurred_at": item.occurred_at.isoformat(),
                "correlation_id": item.correlation_id,
            }
            for item in context.messages
        ),
    )
    decisions = cast(
        tuple[dict[str, object], ...],
        tuple(
            {
                "decision_type": item.decision_type,
                "title": item.title,
                "decision": item.decision,
                "rationale": item.rationale,
                "source": item.source.value,
            }
            for item in context.decisions
        ),
    )
    documents = cast(
        tuple[dict[str, object], ...],
        tuple(
            {
                "document_type": item.document_type.value,
                "revision": item.revision,
                "status": item.status.value,
                "content": item.content,
                "content_hash": item.content_hash,
            }
            for item in context.documents
        ),
    )
    return SpecificationDraftRequest(
        SPECIFICATION_DRAFT_INTERFACE_VERSION,
        correlation_id,
        context.project.id,
        context.project.name,
        context.creation_context.initial_request
        if context.creation_context
        else context.project.name,
        required_visibility(context),
        messages,
        decisions,
        documents,
        feedback,
    )


def approval_message(
    project_name: str, package: DesignPackage, spec_revision: int, agents_revision: int
) -> str:
    return (
        f"{project_name} design ready for approval\n\nRepository: {package.repository_visibility.value.upper()}\n"
        f"Planned milestones: {len(package.planned_milestones)}\n\nSPEC.md: r{spec_revision}\n"
        f"AGENTS.md: r{agents_revision}\n\nSummary:\n{package.design_summary}\n\nGate: {package.approval_gate_id}\n\n"
        "APPROVE\nREQUEST_CHANGES <feedback>"
    )


class SpecificationDraftService:
    def __init__(
        self,
        context: ProjectDesignContextService,
        audit: SpecificationAuditStore,
        provider: SpecificationProvider,
        packages: SQLiteDesignPackageRepository,
        documents: SQLiteProjectDocumentRepository,
        gates: SQLiteHumanGateRepository,
        projects: SQLiteProjectRepository,
        *,
        reasoning_effort: str = "high",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.context, self.audit, self.provider = context, audit, provider
        self.packages, self.documents, self.gates, self.projects = (
            packages,
            documents,
            gates,
            projects,
        )
        self.reasoning_effort, self.clock, self.ids = (
            reasoning_effort,
            clock,
            id_factory,
        )

    def generate(self, project_id: ProjectId, correlation_id: str) -> DesignPackage:
        context = self.context.reconstruct(project_id)
        if context.project.state is not ProjectState.DESIGNING:
            raise ValueError("project must be DESIGNING to formalise its design")
        request = build_specification_request(
            context, correlation_id, self.packages.feedback(project_id)
        )
        request_id = self.ids()
        self.audit.begin_draft(
            request_id=request_id,
            request=request,
            provider=self.provider.provider_name,
            model=self.provider.model,
            reasoning_effort=self.reasoning_effort,
            created_at=self.clock(),
        )
        try:
            draft = self.provider.draft_specification(request)
            if (
                draft.project_id != request.project_id
                or draft.correlation_id != request.correlation_id
                or draft.interface_version != request.interface_version
                or draft.repository_visibility
                is not request.required_repository_visibility
            ):
                raise ArchitectError(
                    ArchitectFailureKind.MALFORMED_RESPONSE,
                    "Architect draft identity or visibility does not match request",
                )
            usage = self.provider.telemetry()
            provider_id = usage.get("provider_response_id")
            self.audit.succeed_draft(
                request_id=request_id,
                response=draft,
                provider_response_id=provider_id
                if isinstance(provider_id, str)
                else None,
                usage=usage,
                completed_at=self.clock(),
            )
        except ArchitectError as error:
            self.audit.fail(
                request_id=request_id, kind=error.kind, completed_at=self.clock()
            )
            raise
        created = self.clock()
        package_id, gate_id = DesignPackageId.generate(), GateId.generate()
        with transaction(self.packages.connection):
            spec = self.documents.create_revision(
                project_id, DocumentType.SPEC, draft.spec_markdown, created, "ARCHITECT"
            )
            agents = self.documents.create_revision(
                project_id,
                DocumentType.AGENTS,
                draft.agents_markdown,
                created,
                "ARCHITECT",
            )
            self.gates.add(
                HumanGate(
                    gate_id,
                    project_id,
                    GateType.DESIGN_APPROVAL,
                    GateState.PENDING,
                    created,
                    title=f"{context.project.name} design ready for approval",
                    prompt=(
                        f"Repository: {draft.repository_visibility.value.upper()}\n"
                        f"Planned milestones: {len(draft.planned_milestones)}\n"
                        f"SPEC.md: r{spec.revision}\n"
                        f"AGENTS.md: r{agents.revision}\n\n"
                        f"Summary:\n{draft.design_summary}\n\n"
                        f"Gate: {gate_id}\n"
                        "Use REQUEST_CHANGES <feedback> when requesting changes."
                    ),
                    expected_response_type=ExpectedResponseType.DESIGN_APPROVAL,
                    options=("APPROVE", "REQUEST_CHANGES"),
                    created_by="SYSTEM",
                    correlation_id=correlation_id,
                    artifact_reference=f"design-package:{package_id}",
                )
            )
            package = self.packages.create(
                project_id=project_id,
                architect_request_id=request_id,
                spec_document_id=spec.id,
                agents_document_id=agents.id,
                visibility=draft.repository_visibility,
                summary=draft.design_summary,
                milestones=draft.planned_milestones,
                assumptions=draft.assumptions,
                issues=draft.non_blocking_issues,
                gate_id=gate_id,
                created_at=created,
                package_id=package_id,
            )
            self.projects.apply_transition(
                ProjectTransitionRequest(
                    project_id,
                    ProjectState.DESIGNING,
                    ProjectState.DESIGN_APPROVAL,
                    "complete design package created",
                    "SYSTEM",
                    None,
                    correlation_id,
                    created,
                )
            )
        return package


class DesignPackageDecisionHandler:
    """Route an authenticated gate command into the atomic M17 decision."""

    def __init__(
        self,
        packages: SQLiteDesignPackageRepository,
        authorised_responder_ids: frozenset[str],
    ) -> None:
        self.packages = packages
        self.authorised = authorised_responder_ids

    def respond(self, command: Command, gate: HumanGate) -> str:
        if command.requested_by not in self.authorised:
            raise PermissionError("responder is not authorised")
        prefix = "design-package:"
        if not gate.artifact_reference or not gate.artifact_reference.startswith(
            prefix
        ):
            raise ValueError("gate has no design package artifact")
        if command.gate_response is None:
            raise ValueError("gate response is missing")
        package_id = DesignPackageId.from_string(
            gate.artifact_reference.removeprefix(prefix)
        )
        package = self.packages.decide(
            package_id=package_id,
            gate_id=gate.id,
            project_id=gate.project_id,
            outcome=command.gate_response,
            feedback=command.gate_feedback,
            responder=command.requested_by,
            message_id=command.source_message_id,
            occurred_at=command.requested_at,
            correlation_id=command.correlation_id,
        )
        return f"Human gate {gate.id} resolved as {package.status.value}."
