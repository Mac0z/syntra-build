"""M14 project creation and deterministic project query use cases."""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from syntra_build.application.commands.models import Command
from syntra_build.application.commands.services import (
    ProjectResolution,
    ProjectSummary,
    ResolutionOutcome,
)
from syntra_build.domain import (
    Project,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    WorkflowEvent,
    WorkflowEventId,
    WorkflowEventSource,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.events import SQLiteWorkflowEventRepository
from syntra_build.infrastructure.persistence.projects import (
    ProjectCreationContext,
    SQLiteProjectRepository,
)

MAX_PROJECT_NAME_LENGTH = 100
MAX_INITIAL_REQUEST_LENGTH = 20_000
RESERVED_CANONICAL_NAMES = frozenset({"syntra", "syntra-build", "system", "internal"})
_LOGGER = logging.getLogger(__name__)


class ProjectCreationFailure(StrEnum):
    INVALID_NAME = "INVALID_NAME"
    RESERVED_NAME = "RESERVED_NAME"
    LOCAL_CONFLICT = "LOCAL_CONFLICT"
    GITHUB_CONFLICT = "GITHUB_CONFLICT"
    GITHUB_UNAVAILABLE = "GITHUB_UNAVAILABLE"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_CONTEXT = "INVALID_CONTEXT"
    PERSISTENCE = "PERSISTENCE"


_MESSAGES = {
    ProjectCreationFailure.INVALID_NAME: "The project name is invalid.",
    ProjectCreationFailure.RESERVED_NAME: "That project name is reserved.",
    ProjectCreationFailure.LOCAL_CONFLICT: "A project with that name already exists.",
    ProjectCreationFailure.GITHUB_CONFLICT: (
        "That name conflicts with an existing GitHub repository."
    ),
    ProjectCreationFailure.GITHUB_UNAVAILABLE: (
        "Project name availability cannot be checked right now. Please try again later."
    ),
    ProjectCreationFailure.INVALID_REQUEST: (
        "The initial request is missing or too long."
    ),
    ProjectCreationFailure.INVALID_CONTEXT: (
        "The messaging conversation identity is unavailable."
    ),
    ProjectCreationFailure.PERSISTENCE: (
        "The project could not be created safely. Please try again."
    ),
}


class ProjectCreationError(RuntimeError):
    def __init__(self, failure: ProjectCreationFailure):
        self.failure = failure
        self.user_message = _MESSAGES[failure]
        super().__init__(self.user_message)


class RepositoryConflictCheckUnavailable(RuntimeError):
    """The read-only provider check could not establish availability."""


class RepositoryNameConflictChecker(Protocol):
    def conflicts(self, canonical_name: str) -> bool: ...


def canonicalize_project_name(display_name: str) -> str:
    """Return a stable lowercase GitHub-style name.

    ASCII letters and digits are retained; every run of whitespace, punctuation,
    or underscores becomes one hyphen. Leading and trailing hyphens are removed.
    """
    normalized = unicodedata.normalize("NFKD", display_name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


def validate_project_name(value: str) -> tuple[str, str]:
    display_name = value.strip()
    if (
        not display_name
        or len(display_name) > MAX_PROJECT_NAME_LENGTH
        or any(unicodedata.category(char) == "Cc" for char in display_name)
    ):
        raise ProjectCreationError(ProjectCreationFailure.INVALID_NAME)
    canonical_name = canonicalize_project_name(display_name)
    if not canonical_name or len(canonical_name) > MAX_PROJECT_NAME_LENGTH:
        raise ProjectCreationError(ProjectCreationFailure.INVALID_NAME)
    if canonical_name in RESERVED_CANONICAL_NAMES:
        raise ProjectCreationError(ProjectCreationFailure.RESERVED_NAME)
    return display_name, canonical_name


@dataclass(frozen=True, slots=True)
class CreatedProject:
    project: Project
    duplicate: bool = False


class ProjectCreationService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        projects: SQLiteProjectRepository,
        events: SQLiteWorkflowEventRepository,
        github_names: RepositoryNameConflictChecker,
        *,
        project_id_factory: Callable[[], ProjectId] = ProjectId.generate,
        event_id_factory: Callable[[], WorkflowEventId] = WorkflowEventId.generate,
    ) -> None:
        self._connection = connection
        self._projects = projects
        self._events = events
        self._github_names = github_names
        self._project_ids = project_id_factory
        self._event_ids = event_id_factory

    def create_from_command(self, command: Command) -> CreatedProject:
        deduplication_key = (
            f"{command.source_platform}:project-create:{command.source_update_id}"
        )
        duplicate = self._events.find_by_external_deduplication_key(deduplication_key)
        if duplicate is not None:
            return CreatedProject(self._projects.get(duplicate.event.project_id), True)

        display_name, canonical_name = validate_project_name(command.project_name or "")
        initial_request = (command.initial_request or "").strip()
        if not initial_request or len(initial_request) > MAX_INITIAL_REQUEST_LENGTH:
            raise ProjectCreationError(ProjectCreationFailure.INVALID_REQUEST)
        if not command.chat_id:
            raise ProjectCreationError(ProjectCreationFailure.INVALID_CONTEXT)
        if self._projects.find_by_canonical_name(canonical_name) is not None:
            raise ProjectCreationError(ProjectCreationFailure.LOCAL_CONFLICT)
        try:
            conflict = self._github_names.conflicts(canonical_name)
        except Exception as error:
            raise ProjectCreationError(
                ProjectCreationFailure.GITHUB_UNAVAILABLE
            ) from error
        if conflict:
            raise ProjectCreationError(ProjectCreationFailure.GITHUB_CONFLICT)

        project_id = self._project_ids()
        project = Project(
            project_id,
            display_name,
            ProjectState.NEW,
            command.requested_at,
            command.requested_at,
            canonical_name=canonical_name,
        )
        event = WorkflowEvent(
            self._event_ids(),
            project_id,
            "PROJECT_CREATION_REQUESTED",
            command.requested_at,
            command.correlation_id,
            payload={"source_message_id": command.source_message_id},
            source=WorkflowEventSource.TELEGRAM,
            external_deduplication_key=deduplication_key,
        )
        try:
            with transaction(self._connection):
                self._projects.add(project)
                self._projects.add_creation_context(
                    ProjectCreationContext(
                        project_id,
                        command.requested_by,
                        initial_request,
                        command.source_platform,
                        command.chat_id,
                        command.thread_id,
                        command.source_update_id,
                        command.source_message_id,
                        command.requested_at,
                    )
                )
                self._events.add(event)
                project = self._projects.apply_transition(
                    ProjectTransitionRequest(
                        project_id,
                        ProjectState.NEW,
                        ProjectState.DESIGNING,
                        "Project creation completed",
                        "HUMAN",
                        command.requested_by,
                        command.correlation_id,
                        command.requested_at,
                        str(event.id),
                    )
                )
                self._events.mark_processed(event.id, command.requested_at)
        except PersistenceError as error:
            # The pre-check is advisory; the unique index remains race authority.
            if self._projects.find_by_canonical_name(canonical_name) is not None:
                raise ProjectCreationError(
                    ProjectCreationFailure.LOCAL_CONFLICT
                ) from error
            raise ProjectCreationError(ProjectCreationFailure.PERSISTENCE) from error
        _LOGGER.info(
            "Project created",
            extra={
                "event": "project_created",
                "metadata": {
                    "project_id": str(project_id),
                    "canonical_name": canonical_name,
                    "owner_id": command.requested_by,
                    "source_platform": command.source_platform,
                    "creation_result": "created",
                },
            },
        )
        return CreatedProject(project)


class SQLiteProjectQueryService:
    def __init__(self, projects: SQLiteProjectRepository):
        self._projects = projects

    @staticmethod
    def _summary(project: Project) -> ProjectSummary:
        return ProjectSummary(project.id, project.name, project.state)

    def list_projects(self) -> tuple[ProjectSummary, ...]:
        return tuple(self._summary(project) for project in self._projects.list_all())

    def resolve_project(self, reference: str) -> ProjectResolution:
        try:
            return ProjectResolution(
                ResolutionOutcome.FOUND,
                self._summary(self._projects.get(ProjectId.from_string(reference))),
            )
        except ValueError, PersistenceError:
            pass
        canonical = canonicalize_project_name(reference.strip())
        project = (
            self._projects.find_by_canonical_name(canonical) if canonical else None
        )
        return ProjectResolution(
            ResolutionOutcome.FOUND if project else ResolutionOutcome.NOT_FOUND,
            self._summary(project) if project else None,
        )

    def get_project_status(self, project_id: ProjectId) -> ProjectSummary:
        return self._summary(self._projects.get(project_id))
