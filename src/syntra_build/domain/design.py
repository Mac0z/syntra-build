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
