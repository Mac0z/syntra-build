"""Narrow service ports consumed by command routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from syntra_build.application.commands.models import Command, InboundMessage
from syntra_build.domain import ProjectId, ProjectState


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    id: ProjectId
    name: str
    state: ProjectState


class ResolutionOutcome(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ProjectResolution:
    outcome: ResolutionOutcome
    project: ProjectSummary | None = None


class ProjectQueryService(Protocol):
    """Resolve only stable IDs or exact canonical names; never fuzzy references."""

    def list_projects(self) -> tuple[ProjectSummary, ...]: ...

    def resolve_project(self, reference: str) -> ProjectResolution: ...

    def get_project_status(self, project_id: ProjectId) -> ProjectSummary: ...


@dataclass(frozen=True, slots=True)
class ProjectCommandResult:
    """Truthful result returned by the future authoritative command service."""

    message: str
    performed: bool


class ProjectCommandService(Protocol):
    def handle(
        self, command: Command, project: ProjectSummary
    ) -> ProjectCommandResult: ...


@dataclass(frozen=True, slots=True)
class CommandAuditRequest:
    event_type: str
    command: Command
    project_id: ProjectId


class CommandAuditSink(Protocol):
    def record(self, request: CommandAuditRequest) -> None: ...


class LocalHealthService(Protocol):
    def current_health(self) -> str: ...


class IntentResolver(Protocol):
    """Future extension used only after deterministic parsing is unrecognized."""

    def resolve(self, message: InboundMessage) -> Command | None: ...
