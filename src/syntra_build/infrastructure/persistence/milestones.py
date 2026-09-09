"""SQLite persistence for authoritative milestone state and history."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from syntra_build.domain._validation import require_utc
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.milestone_state_machine import (
    MilestoneTransitionRequest,
    validate_milestone_transition,
)
from syntra_build.domain.milestones import Milestone, MilestoneState
from syntra_build.infrastructure.persistence.connection import transaction_scope
from syntra_build.infrastructure.persistence.errors import (
    ActiveMilestoneConflictError,
    MilestoneDependencyError,
    MilestoneProjectMismatchError,
    PersistenceError,
    StaleMilestoneStateError,
    UnsatisfiedMilestoneDependenciesError,
)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _required_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MilestoneStateTransition:
    id: str
    milestone_id: MilestoneId
    project_id: ProjectId
    previous_state: MilestoneState
    new_state: MilestoneState
    reason: str
    actor_type: str
    actor_id: str | None
    correlation_id: str
    created_at: datetime
    trigger_event_id: str | None
    metadata: Mapping[str, str | int | bool | None] | None


class SQLiteMilestoneRepository:
    """Store milestones and atomically apply guarded lifecycle transitions."""

    def __init__(self, connection: sqlite3.Connection, id_factory: Callable[[], str]):
        self._connection = connection
        self._id_factory = id_factory

    def add(self, milestone: Milestone) -> None:
        try:
            self._connection.execute(
                """INSERT INTO milestones
                   (id,project_id,sequence_number,code,title,state,resume_state,activity,
                    started_at,completed_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(milestone.id),
                    str(milestone.project_id),
                    milestone.sequence_number,
                    milestone.code,
                    milestone.title,
                    milestone.state.value,
                    milestone.resume_state.value if milestone.resume_state else None,
                    milestone.activity,
                    _timestamp(milestone.started_at) if milestone.started_at else None,
                    _timestamp(milestone.completed_at)
                    if milestone.completed_at
                    else None,
                    _timestamp(milestone.created_at),
                    _timestamp(milestone.updated_at),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PersistenceError(
                "milestone violates persistence integrity"
            ) from error
        except sqlite3.Error as error:
            raise PersistenceError("milestone could not be stored") from error

    def get(
        self, milestone_id: MilestoneId, project_id: ProjectId | None = None
    ) -> Milestone:
        row = self._connection.execute(
            "SELECT * FROM milestones WHERE id=?", (str(milestone_id),)
        ).fetchone()
        if row is None:
            raise PersistenceError("milestone does not exist")
        if project_id is not None and row["project_id"] != str(project_id):
            raise MilestoneProjectMismatchError("milestone does not belong to project")
        return Milestone(
            MilestoneId.from_string(row["id"]),
            ProjectId.from_string(row["project_id"]),
            row["sequence_number"],
            row["code"],
            row["title"],
            MilestoneState(row["state"]),
            _required_datetime(row["created_at"]),
            _required_datetime(row["updated_at"]),
            MilestoneState(row["resume_state"]) if row["resume_state"] else None,
            row["activity"],
            _datetime(row["started_at"]),
            _datetime(row["completed_at"]),
            row["codex_cycle_count"],
            row["ci_rework_count"],
            row["architect_rework_count"],
            row["human_test_rework_count"],
            row["exhaustion_reason"],
        )

    def add_dependency(
        self, milestone_id: MilestoneId, depends_on: MilestoneId
    ) -> None:
        try:
            milestone = self.get(milestone_id)
            dependency = self.get(depends_on)
            if milestone.project_id != dependency.project_id:
                raise MilestoneDependencyError(
                    "milestone dependency must belong to the same project"
                )
            self._connection.execute(
                "INSERT INTO milestone_dependencies VALUES (?,?)",
                (str(milestone_id), str(depends_on)),
            )
        except MilestoneDependencyError:
            raise
        except sqlite3.IntegrityError as error:
            raise MilestoneDependencyError(
                "milestone dependency is duplicate or invalid"
            ) from error

    def is_activation_eligible(
        self, milestone_id: MilestoneId, project_id: ProjectId
    ) -> bool:
        self.get(milestone_id, project_id)
        row = self._connection.execute(
            """SELECT COUNT(*) FROM milestone_dependencies d
               JOIN milestones dependency ON dependency.id=d.depends_on_milestone_id
               WHERE d.milestone_id=?
                 AND (dependency.project_id<>?
                      OR dependency.state<>'COMPLETE')""",
            (str(milestone_id), str(project_id)),
        ).fetchone()
        return int(row[0]) == 0

    def apply_transition(self, request: MilestoneTransitionRequest) -> Milestone:
        try:
            with transaction_scope(self._connection):
                row = self._connection.execute(
                    "SELECT * FROM milestones WHERE id=?", (str(request.milestone_id),)
                ).fetchone()
                if row is None:
                    raise PersistenceError("milestone does not exist")
                if row["project_id"] != str(request.project_id):
                    raise MilestoneProjectMismatchError(
                        "milestone does not belong to project"
                    )
                persisted = MilestoneState(row["state"])
                if persisted is not request.expected_state:
                    raise StaleMilestoneStateError(
                        "milestone state changed since it was read"
                    )
                created_at = _datetime(row["created_at"])
                if created_at is None or request.occurred_at < created_at:
                    raise PersistenceError(
                        "transition timestamp cannot precede milestone creation"
                    )
                resume = (
                    MilestoneState(row["resume_state"]) if row["resume_state"] else None
                )
                validate_milestone_transition(persisted, request.target_state, resume)
                if (
                    persisted is MilestoneState.PENDING
                    and request.target_state is MilestoneState.READY
                    and not self.is_activation_eligible(
                        request.milestone_id, request.project_id
                    )
                ):
                    raise UnsatisfiedMilestoneDependenciesError(
                        "milestone dependencies are not complete"
                    )
                new_resume = (
                    persisted
                    if request.target_state is MilestoneState.BLOCKED
                    else resume
                )
                if persisted is MilestoneState.BLOCKED:
                    new_resume = None
                started_at = row["started_at"]
                if (
                    request.target_state not in {MilestoneState.PENDING}
                    and started_at is None
                ):
                    started_at = _timestamp(request.occurred_at)
                completed_at = (
                    _timestamp(request.occurred_at)
                    if request.target_state is MilestoneState.COMPLETE
                    else row["completed_at"]
                )
                try:
                    cursor = self._connection.execute(
                        """UPDATE milestones
                           SET state=?,resume_state=?,started_at=?,completed_at=?,
                               updated_at=?
                           WHERE id=? AND project_id=? AND state=?""",
                        (
                            request.target_state.value,
                            new_resume.value if new_resume else None,
                            started_at,
                            completed_at,
                            _timestamp(request.occurred_at),
                            str(request.milestone_id),
                            str(request.project_id),
                            persisted.value,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    if "milestones.project_id" in str(error):
                        raise ActiveMilestoneConflictError(
                            "project already has an active milestone"
                        ) from error
                    raise
                if cursor.rowcount != 1:
                    raise StaleMilestoneStateError(
                        "milestone state changed while transitioning"
                    )
                self._connection.execute(
                    """INSERT INTO state_transitions
                    (id,entity_type,entity_id,project_id,milestone_id,previous_state,new_state,reason,
                     trigger_event_id,actor_type,actor_id,correlation_id,created_at,
                     metadata_json)
                    VALUES (?,'MILESTONE',?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self._id_factory(),
                        str(request.milestone_id),
                        str(request.project_id),
                        str(request.milestone_id),
                        persisted.value,
                        request.target_state.value,
                        request.reason,
                        request.trigger_event_id,
                        request.actor_type,
                        request.actor_id,
                        request.correlation_id,
                        _timestamp(request.occurred_at),
                        json.dumps(request.metadata, sort_keys=True)
                        if request.metadata is not None
                        else None,
                    ),
                )
        except PersistenceError, ValueError:
            raise
        except sqlite3.Error as error:
            raise PersistenceError(
                "milestone transition could not be persisted"
            ) from error
        return self.get(request.milestone_id, request.project_id)

    def apply_rework_transition(
        self,
        request: MilestoneTransitionRequest,
        counter: str,
        limit: int,
        *,
        exhaustion_reason: str,
    ) -> Milestone:
        """Atomically increment one logical-cycle counter and transition or block."""
        columns = {
            "codex": "codex_cycle_count",
            "ci": "ci_rework_count",
            "architect": "architect_rework_count",
            "human_test": "human_test_rework_count",
        }
        if counter not in columns or type(limit) is not int or limit < 1:
            raise ValueError("invalid rework counter policy")
        column = columns[counter]
        with transaction_scope(self._connection):
            row = self._connection.execute(
                f"SELECT state,{column} AS count FROM milestones "
                "WHERE id=? AND project_id=?",
                (str(request.milestone_id), str(request.project_id)),
            ).fetchone()
            if row is None:
                raise MilestoneProjectMismatchError(
                    "milestone does not belong to project"
                )
            count = int(row["count"])
            target = request.target_state
            if count >= limit:
                target = MilestoneState.BLOCKED
            effective = MilestoneTransitionRequest(
                request.milestone_id,
                request.project_id,
                request.expected_state,
                target,
                request.reason,
                request.actor_type,
                request.actor_id,
                request.correlation_id,
                request.occurred_at,
                request.trigger_event_id,
                request.metadata,
            )
            self.apply_transition(effective)
            self._connection.execute(
                f"UPDATE milestones SET {column}=?,exhaustion_reason=? WHERE id=?",
                (
                    count if target is MilestoneState.BLOCKED else count + 1,
                    exhaustion_reason if target is MilestoneState.BLOCKED else None,
                    str(request.milestone_id),
                ),
            )
        return self.get(request.milestone_id, request.project_id)

    def update_activity(
        self,
        milestone_id: MilestoneId,
        project_id: ProjectId,
        activity: str | None,
        updated_at: datetime,
    ) -> Milestone:
        require_utc(updated_at, "updated_at")
        cursor = self._connection.execute(
            "UPDATE milestones SET activity=?,updated_at=? WHERE id=? AND project_id=?",
            (activity, _timestamp(updated_at), str(milestone_id), str(project_id)),
        )
        if cursor.rowcount != 1:
            raise MilestoneProjectMismatchError("milestone does not belong to project")
        return self.get(milestone_id, project_id)

    def transitions(
        self, milestone_id: MilestoneId, project_id: ProjectId
    ) -> tuple[MilestoneStateTransition, ...]:
        self.get(milestone_id, project_id)
        rows = self._connection.execute(
            """SELECT * FROM state_transitions
               WHERE entity_type='MILESTONE' AND milestone_id=? AND project_id=?
               ORDER BY created_at,id""",
            (str(milestone_id), str(project_id)),
        ).fetchall()
        return tuple(
            MilestoneStateTransition(
                row["id"],
                MilestoneId.from_string(row["milestone_id"]),
                ProjectId.from_string(row["project_id"]),
                MilestoneState(row["previous_state"]),
                MilestoneState(row["new_state"]),
                row["reason"],
                row["actor_type"],
                row["actor_id"],
                row["correlation_id"],
                _required_datetime(row["created_at"]),
                row["trigger_event_id"],
                json.loads(row["metadata_json"])
                if row["metadata_json"] is not None
                else None,
            )
            for row in rows
        )
