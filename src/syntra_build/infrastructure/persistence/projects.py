"""SQLite persistence for authoritative project state and history."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from syntra_build.domain import (
    Project,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
)
from syntra_build.domain._validation import require_utc
from syntra_build.domain.project_state_machine import validate_transition
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import (
    PersistenceError,
    StaleProjectStateError,
)


@dataclass(frozen=True, slots=True)
class ProjectStateTransition:
    id: str
    project_id: ProjectId
    previous_state: ProjectState
    new_state: ProjectState
    reason: str
    actor_type: str
    actor_id: str | None
    correlation_id: str
    created_at: datetime
    trigger_event_id: str | None


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC)


class SQLiteProjectRepository:
    """Store projects and apply each validated state change in one transaction."""

    def __init__(self, connection: sqlite3.Connection, id_factory: Callable[[], str]):
        self._connection = connection
        self._id_factory = id_factory

    def add(self, project: Project) -> None:
        changed_at = project.last_state_change_at or project.created_at
        try:
            self._connection.execute(
                """INSERT INTO projects
                   (id,name,state,resume_state,activity,created_at,updated_at,last_state_change_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    str(project.id),
                    project.name,
                    project.state.value,
                    project.resume_state.value if project.resume_state else None,
                    project.activity,
                    _timestamp(project.created_at),
                    _timestamp(project.updated_at),
                    _timestamp(changed_at),
                ),
            )
        except sqlite3.Error as error:
            raise PersistenceError("project could not be stored") from error

    def get(self, project_id: ProjectId) -> Project:
        row = self._connection.execute(
            "SELECT * FROM projects WHERE id = ?", (str(project_id),)
        ).fetchone()
        if row is None:
            raise PersistenceError("project does not exist")
        return Project(
            id=ProjectId.from_string(row["id"]),
            name=row["name"],
            state=ProjectState(row["state"]),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
            resume_state=ProjectState(row["resume_state"])
            if row["resume_state"]
            else None,
            activity=row["activity"],
            last_state_change_at=_datetime(row["last_state_change_at"]),
        )

    def apply_transition(self, request: ProjectTransitionRequest) -> Project:
        """Conditionally update state and append history in the same transaction."""
        try:
            with transaction(self._connection):
                row = self._connection.execute(
                    "SELECT state,resume_state FROM projects WHERE id = ?",
                    (str(request.project_id),),
                ).fetchone()
                if row is None:
                    raise PersistenceError("project does not exist")
                persisted = ProjectState(row["state"])
                if persisted is not request.expected_state:
                    raise StaleProjectStateError(
                        "project state changed since it was read"
                    )
                old_resume = (
                    ProjectState(row["resume_state"]) if row["resume_state"] else None
                )
                created_at = _datetime(
                    self._connection.execute(
                        "SELECT created_at FROM projects WHERE id = ?",
                        (str(request.project_id),),
                    ).fetchone()[0]
                )
                if request.occurred_at < created_at:
                    raise PersistenceError(
                        "transition timestamp cannot precede project creation"
                    )
                validate_transition(persisted, request.target_state, old_resume)
                new_resume = (
                    persisted
                    if request.target_state is ProjectState.PAUSED
                    else old_resume
                )
                if persisted is ProjectState.PAUSED and request.target_state not in {
                    ProjectState.BLOCKED,
                    ProjectState.CANCELLED,
                }:
                    new_resume = None
                cursor = self._connection.execute(
                    """UPDATE projects
                       SET state=?, resume_state=?, updated_at=?,
                           last_state_change_at=?
                       WHERE id=? AND state=?""",
                    (
                        request.target_state.value,
                        new_resume.value if new_resume else None,
                        _timestamp(request.occurred_at),
                        _timestamp(request.occurred_at),
                        str(request.project_id),
                        persisted.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleProjectStateError(
                        "project state changed while transitioning"
                    )
                self._connection.execute(
                    """INSERT INTO state_transitions
                       (id,entity_type,entity_id,project_id,previous_state,new_state,reason,
                        trigger_event_id,actor_type,actor_id,correlation_id,created_at)
                       VALUES (?,'PROJECT',?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self._id_factory(),
                        str(request.project_id),
                        str(request.project_id),
                        persisted.value,
                        request.target_state.value,
                        request.reason,
                        request.trigger_event_id,
                        request.actor_type,
                        request.actor_id,
                        request.correlation_id,
                        _timestamp(request.occurred_at),
                    ),
                )
        except StaleProjectStateError, PersistenceError:
            raise
        except sqlite3.Error as error:
            raise PersistenceError(
                "project transition could not be persisted"
            ) from error
        return self.get(request.project_id)

    def update_activity(
        self, project_id: ProjectId, activity: str | None, updated_at: datetime
    ) -> Project:
        require_utc(updated_at, "updated_at")
        cursor = self._connection.execute(
            "UPDATE projects SET activity=?, updated_at=? WHERE id=?",
            (activity, _timestamp(updated_at), str(project_id)),
        )
        if cursor.rowcount != 1:
            raise PersistenceError("project does not exist")
        return self.get(project_id)

    def transitions(self, project_id: ProjectId) -> tuple[ProjectStateTransition, ...]:
        rows = self._connection.execute(
            "SELECT * FROM state_transitions WHERE project_id=? ORDER BY created_at,id",
            (str(project_id),),
        ).fetchall()
        return tuple(
            ProjectStateTransition(
                id=row["id"],
                project_id=ProjectId.from_string(row["project_id"]),
                previous_state=ProjectState(row["previous_state"]),
                new_state=ProjectState(row["new_state"]),
                reason=row["reason"],
                actor_type=row["actor_type"],
                actor_id=row["actor_id"],
                correlation_id=row["correlation_id"],
                created_at=_datetime(row["created_at"]),
                trigger_event_id=row["trigger_event_id"],
            )
            for row in rows
        )
