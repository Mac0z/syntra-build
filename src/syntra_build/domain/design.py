"""Typed, provider-independent project design artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import (
    GateId,
    MessageId,
    MilestoneId,
    ProjectDecisionId,
    ProjectDocumentId,
    ProjectId,
)
from syntra_build.domain.projects import Project, ProjectCreationContext


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class DocumentType(StrEnum):
    SPEC = "SPEC"
    AGENTS = "AGENTS"


class DocumentStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class DecisionSource(StrEnum):
    HUMAN = "HUMAN"
    ARCHITECT = "ARCHITECT"
    POLICY = "POLICY"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True, slots=True)
class DesignMessage:
    id: MessageId
    project_id: ProjectId
    direction: MessageDirection
    platform: str
    chat_id: str
    message_type: str
    text: str | None
    occurred_at: datetime
    correlation_id: str
    milestone_id: MilestoneId | None = None
    gate_id: GateId | None = None
    external_message_id: str | None = None
    thread_id: str | None = None
    sender_id: str | None = None
    attachments_json: str | None = None
    reply_to_message_id: str | None = None
    raw_metadata_json: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, MessageId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_enum(self.direction, MessageDirection, "direction")
        for value, name in (
            (self.platform, "platform"),
            (self.chat_id, "chat_id"),
            (self.message_type, "message_type"),
            (self.correlation_id, "correlation_id"),
        ):
            require_text(value, name)
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("text must be a string or None")
        require_utc(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class ProjectDocument:
    id: ProjectDocumentId
    project_id: ProjectId
    document_type: DocumentType
    revision: int
    status: DocumentStatus
    content: str
    content_hash: str
    created_at: datetime
    created_by: str
    approved_at: datetime | None = None
    approved_by: str | None = None
    supersedes_document_id: ProjectDocumentId | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, ProjectDocumentId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_enum(self.document_type, DocumentType, "document_type")
        require_enum(self.status, DocumentStatus, "status")
        if not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        require_text(self.content_hash, "content_hash")
        require_text(self.created_by, "created_by")
        require_utc(self.created_at, "created_at")
        if self.approved_at is not None:
            require_utc(self.approved_at, "approved_at")
        if self.status is DocumentStatus.APPROVED:
            if self.approved_at is None:
                raise DomainValidationError("approved document requires approved_at")
            require_text(self.approved_by, "approved_by")


@dataclass(frozen=True, slots=True)
class ProjectDecision:
    id: ProjectDecisionId
    project_id: ProjectId
    decision_type: str
    title: str
    decision: str
    rationale: str
    source: DecisionSource
    created_at: datetime
    created_by: str
    milestone_id: MilestoneId | None = None
    superseded_by_decision_id: ProjectDecisionId | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, ProjectDecisionId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        for value, name in (
            (self.decision_type, "decision_type"),
            (self.title, "title"),
            (self.decision, "decision"),
            (self.rationale, "rationale"),
            (self.created_by, "created_by"),
        ):
            require_text(value, name)
        require_enum(self.source, DecisionSource, "source")
        require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ProjectDesignContext:
    project: Project
    creation_context: ProjectCreationContext | None
    messages: tuple[DesignMessage, ...]
    decisions: tuple[ProjectDecision, ...]
    documents: tuple[ProjectDocument, ...]


ARCHITECT_INTERFACE_VERSION = "1.0"


class ArchitectDesignMode(StrEnum):
    ASK_USER = "ASK_USER"
    PROPOSE_DESIGN = "PROPOSE_DESIGN"
    READY_TO_DRAFT = "READY_TO_DRAFT"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ArchitectDesignRequest:
    """Provider-neutral, reconstructable input to one design operation."""

    interface_version: str
    correlation_id: str
    project_id: ProjectId
    project_name: str
    initial_request: str
    conversation_context: tuple[dict[str, object], ...]
    known_decisions: tuple[dict[str, object], ...]
    open_questions: tuple[str, ...]
    document_context: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.interface_version != ARCHITECT_INTERFACE_VERSION:
            raise DomainValidationError("unsupported Architect interface version")
        require_identifier(self.project_id, ProjectId, "project_id")
        for value, name in (
            (self.correlation_id, "correlation_id"),
            (self.project_name, "project_name"),
            (self.initial_request, "initial_request"),
        ):
            require_text(value, name)
        if any(
            not isinstance(value, str) or not value.strip()
            for value in self.open_questions
        ):
            raise DomainValidationError("open_questions must contain non-empty strings")

    def to_dict(self) -> dict[str, object]:
        return {
            "interface_version": self.interface_version,
            "correlation_id": self.correlation_id,
            "project_id": str(self.project_id),
            "project_name": self.project_name,
            "initial_request": self.initial_request,
            "conversation_context": list(self.conversation_context),
            "known_decisions": list(self.known_decisions),
            "open_questions": list(self.open_questions),
            "document_context": list(self.document_context),
        }


@dataclass(frozen=True, slots=True)
class ArchitectProposedDecision:
    """An untrusted Architect proposal, never an authoritative project decision."""

    decision_type: str
    title: str
    proposal: str
    rationale: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.decision_type, "decision_type"),
            (self.title, "title"),
            (self.proposal, "proposal"),
            (self.rationale, "rationale"),
        ):
            require_text(value, name)

    @classmethod
    def from_dict(cls, value: object) -> ArchitectProposedDecision:
        fields = {"decision_type", "title", "proposal", "rationale"}
        if not isinstance(value, dict) or set(value) != fields:
            raise DomainValidationError("malformed Architect proposed decision")
        if any(not isinstance(value[field], str) for field in fields):
            raise DomainValidationError(
                "Architect proposed decision fields must be strings"
            )
        return cls(
            value["decision_type"],
            value["title"],
            value["proposal"],
            value["rationale"],
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "decision_type": self.decision_type,
            "title": self.title,
            "proposal": self.proposal,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ArchitectDesignResponse:
    """Validated advisory output; proposals are never accepted decisions."""

    interface_version: str
    correlation_id: str
    project_id: ProjectId
    mode: ArchitectDesignMode
    message: str
    proposed_decisions: tuple[ArchitectProposedDecision, ...]
    open_questions: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.interface_version != ARCHITECT_INTERFACE_VERSION:
            raise DomainValidationError("unsupported Architect interface version")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_enum(self.mode, ArchitectDesignMode, "mode")
        require_text(self.correlation_id, "correlation_id")
        require_text(self.message, "message")
        if any(
            not isinstance(item, ArchitectProposedDecision)
            for item in self.proposed_decisions
        ):
            raise DomainValidationError(
                "proposed_decisions must contain ArchitectProposedDecision objects"
            )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in self.open_questions
        ):
            raise DomainValidationError("open_questions must contain non-empty strings")

    @classmethod
    def from_dict(cls, value: object) -> ArchitectDesignResponse:
        if not isinstance(value, dict) or set(value) != {
            "interface_version",
            "correlation_id",
            "project_id",
            "mode",
            "message",
            "proposed_decisions",
            "open_questions",
        }:
            raise DomainValidationError("malformed Architect design response")
        proposals, questions = value["proposed_decisions"], value["open_questions"]
        if not isinstance(proposals, list) or not isinstance(questions, list):
            raise DomainValidationError("malformed Architect response collections")
        try:
            return cls(
                str(value["interface_version"]),
                str(value["correlation_id"]),
                ProjectId.from_string(str(value["project_id"])),
                ArchitectDesignMode(str(value["mode"])),
                str(value["message"]),
                tuple(ArchitectProposedDecision.from_dict(item) for item in proposals),
                tuple(questions),
            )
        except (TypeError, ValueError) as error:
            raise DomainValidationError(
                "malformed Architect design response"
            ) from error

    def to_dict(self) -> dict[str, object]:
        return {
            "interface_version": self.interface_version,
            "correlation_id": self.correlation_id,
            "project_id": str(self.project_id),
            "mode": self.mode.value,
            "message": self.message,
            "proposed_decisions": [item.to_dict() for item in self.proposed_decisions],
            "open_questions": list(self.open_questions),
        }
