"""Structured project-design context reconstruction for later application use."""

from __future__ import annotations

from typing import Protocol

from syntra_build.domain import (
    DesignMessage,
    ProjectCreationContext,
    ProjectDecision,
    ProjectDesignContext,
    ProjectDocument,
    ProjectId,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository


class DesignMessageReader(Protocol):
    def for_project(self, project_id: ProjectId) -> tuple[DesignMessage, ...]: ...


class ProjectDecisionReader(Protocol):
    def for_project(
        self, project_id: ProjectId, *, active_only: bool = False
    ) -> tuple[ProjectDecision, ...]: ...


class ProjectDocumentReader(Protocol):
    def current_for_project(
        self, project_id: ProjectId
    ) -> tuple[ProjectDocument, ...]: ...


class ProjectDesignContextService:
    """Reconstruct current design inputs without provider memory or event replay."""

    def __init__(
        self,
        projects: SQLiteProjectRepository,
        messages: DesignMessageReader,
        decisions: ProjectDecisionReader,
        documents: ProjectDocumentReader,
    ) -> None:
        self._projects = projects
        self._messages = messages
        self._decisions = decisions
        self._documents = documents

    def reconstruct(self, project_id: ProjectId) -> ProjectDesignContext:
        return ProjectDesignContext(
            self._projects.get(project_id),
            self._creation_context(project_id),
            self._messages.for_project(project_id),
            self._decisions.for_project(project_id, active_only=True),
            self._documents.current_for_project(project_id),
        )

    def _creation_context(self, project_id: ProjectId) -> ProjectCreationContext | None:
        """Include creation context when the project was created through M14."""
        try:
            return self._projects.get_creation_context(project_id)
        except PersistenceError:
            # Pre-M14/test projects may legitimately lack this one-to-one record.
            return None
