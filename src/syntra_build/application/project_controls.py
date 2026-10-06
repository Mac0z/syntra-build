"""Authoritative human project controls and their durable command audit."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from syntra_build.application.commands.models import Command, CommandType
from syntra_build.application.commands.services import (
    CommandAuditRequest,
    ProjectCommandResult,
    ProjectSummary,
)
from syntra_build.domain import (
    DuplicateEvent,
    InsertedEvent,
    InvalidProjectTransitionError,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    WorkflowEvent,
    WorkflowEventId,
    WorkflowEventSource,
    is_transition_allowed,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import (
    PersistenceError,
    StaleProjectStateError,
)
from syntra_build.infrastructure.persistence.events import (
    SQLiteWorkflowEventRepository,
)
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository


def command_audit_deduplication_key(command: Command) -> str:
    """Bind one external source update to one durable command request."""
    return (
        f"{command.source_platform.casefold()}:"
        f"state-change-command:{command.source_update_id}"
    )


def _event_source(command: Command) -> WorkflowEventSource:
    try:
        return WorkflowEventSource(command.source_platform.upper())
    except ValueError:
        return WorkflowEventSource.INTERNAL


def _audit_matches(
    event: WorkflowEvent, command: Command, project_id: ProjectId
) -> bool:
    payload = event.payload or {}
    return (
        event.event_type == "STATE_CHANGE_COMMAND_REQUESTED"
        and event.project_id == project_id
        and event.external_deduplication_key == command_audit_deduplication_key(command)
        and payload.get("command") == command.type.value
        and payload.get("requested_by") == command.requested_by
        and payload.get("source_platform") == command.source_platform
        and payload.get("source_update_id") == command.source_update_id
        and payload.get("source_message_id") == command.source_message_id
    )


class WorkflowEventCommandAuditSink:
    """Persist state-changing command intent before command execution."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        events: SQLiteWorkflowEventRepository,
        *,
        event_id_factory: Callable[[], WorkflowEventId] = WorkflowEventId.generate,
    ) -> None:
        self._connection = connection
        self._events = events
        self._event_ids = event_id_factory

    def record(self, request: CommandAuditRequest) -> None:
        command = request.command
        event = WorkflowEvent(
            self._event_ids(),
            request.project_id,
            request.event_type,
            command.requested_at,
            command.correlation_id,
            payload={
                "command": command.type.value,
                "requested_by": command.requested_by,
                "source_platform": command.source_platform,
                "source_update_id": command.source_update_id,
                "source_message_id": command.source_message_id,
            },
            source=_event_source(command),
            external_deduplication_key=command_audit_deduplication_key(command),
        )
        with transaction(self._connection):
            stored = self._events.add(event)
            if isinstance(stored, InsertedEvent):
                self._events.mark_processed(event.id, command.requested_at)
            elif isinstance(stored, DuplicateEvent) and not _audit_matches(
                stored.record.event, command, request.project_id
            ):
                raise PersistenceError(
                    "command source identity conflicts with existing audit evidence"
                )


class AuthoritativeProjectCommandService:
    """Apply pause, resume, and cancellation through the project state machine."""

    def __init__(
        self,
        projects: SQLiteProjectRepository,
        events: SQLiteWorkflowEventRepository,
    ) -> None:
        self._projects = projects
        self._events = events

    def handle(self, command: Command, project: ProjectSummary) -> ProjectCommandResult:
        current = self._projects.get(project.id)
        audit = self._events.find_by_external_deduplication_key(
            command_audit_deduplication_key(command)
        )
        if audit is None or not _audit_matches(audit.event, command, current.id):
            return ProjectCommandResult(
                f"Project {current.name} was not changed because its command "
                "audit could not be verified.",
                False,
            )

        target = self._target(
            command, current.state, current.resume_state, current.name
        )
        if isinstance(target, ProjectCommandResult):
            return target

        action = {
            CommandType.PAUSE_PROJECT: "paused",
            CommandType.RESUME_PROJECT: "resumed",
            CommandType.CANCEL_PROJECT: "cancelled",
        }.get(command.type)
        if action is None:
            return ProjectCommandResult("Project command is not supported.", False)

        try:
            changed = self._projects.apply_transition(
                ProjectTransitionRequest(
                    current.id,
                    current.state,
                    target,
                    f"Human requested project {action}",
                    "HUMAN",
                    command.requested_by,
                    audit.event.correlation_id,
                    command.requested_at,
                    str(audit.event.id),
                )
            )
        except StaleProjectStateError:
            return ProjectCommandResult(
                f"Project {current.name} changed state while the {action} command "
                "was processed. Please retry.",
                False,
            )
        except InvalidProjectTransitionError:
            return ProjectCommandResult(
                f"Project {current.name} cannot be {action} from "
                f"{current.state.value}.",
                False,
            )
        except PersistenceError:
            return ProjectCommandResult(
                f"Project {current.name} could not be {action} safely. Please retry.",
                False,
            )

        if command.type is CommandType.PAUSE_PROJECT:
            return ProjectCommandResult(
                f"Project {changed.name} paused. Resume target: {current.state.value}.",
                True,
            )
        if command.type is CommandType.RESUME_PROJECT:
            return ProjectCommandResult(
                f"Project {changed.name} resumed to {changed.state.value}.", True
            )
        return ProjectCommandResult(f"Project {changed.name} cancelled.", True)

    @staticmethod
    def _target(
        command: Command,
        state: ProjectState,
        resume_state: ProjectState | None,
        name: str,
    ) -> ProjectState | ProjectCommandResult:
        if command.type is CommandType.PAUSE_PROJECT:
            if state is ProjectState.PAUSED:
                return ProjectCommandResult(f"Project {name} is already paused.", False)
            if not is_transition_allowed(state, ProjectState.PAUSED):
                return ProjectCommandResult(
                    f"Project {name} cannot be paused from {state.value}.", False
                )
            return ProjectState.PAUSED

        if command.type is CommandType.RESUME_PROJECT:
            if state is not ProjectState.PAUSED:
                return ProjectCommandResult(
                    f"Project {name} is not paused (state: {state.value}).", False
                )
            if (
                resume_state is None
                or resume_state is ProjectState.PAUSED
                or not is_transition_allowed(ProjectState.PAUSED, resume_state)
                or not is_transition_allowed(resume_state, ProjectState.PAUSED)
            ):
                return ProjectCommandResult(
                    f"Project {name} cannot be resumed because no valid resume "
                    "state is recorded.",
                    False,
                )
            return resume_state

        if command.type is CommandType.CANCEL_PROJECT:
            if state is ProjectState.CANCELLED:
                return ProjectCommandResult(
                    f"Project {name} is already cancelled.", False
                )
            if not is_transition_allowed(state, ProjectState.CANCELLED):
                return ProjectCommandResult(
                    f"Project {name} cannot be cancelled from {state.value}.", False
                )
            return ProjectState.CANCELLED

        return ProjectCommandResult("Project command is not supported.", False)
