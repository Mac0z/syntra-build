"""Strict provider-neutral contracts and durable entities for M17."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from syntra_build.domain._validation import (
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import (
    DesignPackageId,
    GateId,
    ProjectDocumentId,
    ProjectId,
)
from syntra_build.domain.projects import RepositoryVisibility

SPECIFICATION_DRAFT_INTERFACE_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class PlannedMilestone:
    code: str
    title: str

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.title, "title")

    @classmethod
    def from_dict(cls, value: object) -> PlannedMilestone:
        if not isinstance(value, dict) or set(value) != {"code", "title"}:
            raise DomainValidationError("malformed planned milestone")
        if not all(isinstance(value[key], str) for key in value):
            raise DomainValidationError("planned milestone fields must be strings")
        return cls(value["code"], value["title"])

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "title": self.title}


@dataclass(frozen=True, slots=True)
class SpecificationDraftRequest:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    project_name: str
    initial_request: str
    required_repository_visibility: RepositoryVisibility
    conversation_context: tuple[dict[str, object], ...]
    known_decisions: tuple[dict[str, object], ...]
    current_document_context: tuple[dict[str, object], ...]
    prior_change_feedback: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.interface_version != SPECIFICATION_DRAFT_INTERFACE_VERSION:
            raise DomainValidationError(
                "unsupported specification draft interface version"
            )
        require_identifier(self.project_id, ProjectId, "project_id")
        for value, name in (
            (self.correlation_id, "correlation_id"),
            (self.project_name, "project_name"),
            (self.initial_request, "initial_request"),
        ):
            require_text(value, name)
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.prior_change_feedback
        ):
            raise DomainValidationError(
                "prior_change_feedback must contain non-empty strings"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "interface_version": self.interface_version,
            "correlation_id": self.correlation_id,
            "project_id": str(self.project_id),
            "project_name": self.project_name,
            "initial_request": self.initial_request,
            "required_repository_visibility": self.required_repository_visibility.value,
            "conversation_context": list(self.conversation_context),
            "known_decisions": list(self.known_decisions),
            "current_document_context": list(self.current_document_context),
            "prior_change_feedback": list(self.prior_change_feedback),
        }


@dataclass(frozen=True, slots=True)
class SpecificationDraft:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    design_summary: str
    repository_visibility: RepositoryVisibility
    spec_markdown: str
    agents_markdown: str
    assumptions: tuple[str, ...]
    non_blocking_issues: tuple[str, ...]
    planned_milestones: tuple[PlannedMilestone, ...]

    def __post_init__(self) -> None:
        if self.interface_version != SPECIFICATION_DRAFT_INTERFACE_VERSION:
            raise DomainValidationError(
                "unsupported specification draft interface version"
            )
        require_identifier(self.project_id, ProjectId, "project_id")
        for value, name in (
            (self.correlation_id, "correlation_id"),
            (self.design_summary, "design_summary"),
            (self.spec_markdown, "spec_markdown"),
            (self.agents_markdown, "agents_markdown"),
        ):
            require_text(value, name)
        for values, name in (
            (self.assumptions, "assumptions"),
            (self.non_blocking_issues, "non_blocking_issues"),
        ):
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise DomainValidationError(f"{name} must contain non-empty strings")
        if any(
            not isinstance(item, PlannedMilestone) for item in self.planned_milestones
        ):
            raise DomainValidationError(
                "planned_milestones must contain typed milestones"
            )

    @classmethod
    def from_dict(cls, value: object) -> SpecificationDraft:
        fields = {
            "interface_version",
            "correlation_id",
            "project_id",
            "design_summary",
            "repository_visibility",
            "spec_markdown",
            "agents_markdown",
            "assumptions",
            "non_blocking_issues",
            "planned_milestones",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise DomainValidationError("malformed specification draft")
        for name in ("assumptions", "non_blocking_issues", "planned_milestones"):
            if not isinstance(value[name], list):
                raise DomainValidationError("malformed specification draft collections")
        try:
            return cls(
                str(value["interface_version"]),
                str(value["correlation_id"]),
                ProjectId.from_string(str(value["project_id"])),
                str(value["design_summary"]),
                RepositoryVisibility(str(value["repository_visibility"])),
                str(value["spec_markdown"]),
                str(value["agents_markdown"]),
                tuple(value["assumptions"]),
                tuple(value["non_blocking_issues"]),
                tuple(
                    PlannedMilestone.from_dict(x) for x in value["planned_milestones"]
                ),
            )
        except (TypeError, ValueError) as error:
            raise DomainValidationError("malformed specification draft") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "interface_version": self.interface_version,
            "correlation_id": self.correlation_id,
            "project_id": str(self.project_id),
            "design_summary": self.design_summary,
            "repository_visibility": self.repository_visibility.value,
            "spec_markdown": self.spec_markdown,
            "agents_markdown": self.agents_markdown,
            "assumptions": list(self.assumptions),
            "non_blocking_issues": list(self.non_blocking_issues),
            "planned_milestones": [item.to_dict() for item in self.planned_milestones],
        }


class DesignPackageStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class DesignPackage:
    id: DesignPackageId
    project_id: ProjectId
    architect_request_id: str
    spec_document_id: ProjectDocumentId
    agents_document_id: ProjectDocumentId
    repository_visibility: RepositoryVisibility
    design_summary: str
    planned_milestones: tuple[PlannedMilestone, ...]
    assumptions: tuple[str, ...]
    non_blocking_issues: tuple[str, ...]
    status: DesignPackageStatus
    approval_gate_id: GateId
    created_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    rejected_at: datetime | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, DesignPackageId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_identifier(self.spec_document_id, ProjectDocumentId, "spec_document_id")
        require_identifier(
            self.agents_document_id, ProjectDocumentId, "agents_document_id"
        )
        require_identifier(self.approval_gate_id, GateId, "approval_gate_id")
        require_text(self.architect_request_id, "architect_request_id")
        require_text(self.design_summary, "design_summary")
        require_utc(self.created_at, "created_at")
