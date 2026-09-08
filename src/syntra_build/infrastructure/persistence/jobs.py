# ruff: noqa: E501
"""Transactional SQLite persistence for jobs and immutable attempts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from syntra_build.domain.errors import InvalidRetryMetadataError
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.job_state_machine import (
    TERMINAL_JOB_STATES,
    JobTransitionRequest,
    validate_job_transition,
)
from syntra_build.domain.jobs import (
    Job,
    JobAttempt,
    JobAttemptState,
    JobState,
    WorkerClass,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import (
    AttemptLimitExhaustedError,
    ImmutableTerminalAttemptError,
    InvalidAttemptError,
    JobMilestoneProjectMismatchError,
    JobProjectMismatchError,
    PersistenceError,
    StaleJobStateError,
    TerminalJobMutationError,
)


def _ts(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _required_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _json(value: Mapping[str, object] | None) -> str:
    return json.dumps(dict(value or {}), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class JobStateTransition:
    id: str
    job_id: JobId
    project_id: ProjectId
    milestone_id: MilestoneId | None
    previous_state: JobState
    new_state: JobState
    reason: str
    actor_type: str
    actor_id: str | None
    correlation_id: str
    created_at: datetime
    trigger_event_id: str | None
    metadata: Mapping[str, object] | None


class SQLiteJobRepository:
    def __init__(self, connection: sqlite3.Connection, id_factory: Callable[[], str]):
        self._connection, self._id_factory = connection, id_factory

    def add(self, job: Job) -> None:
        try:
            self._connection.execute(
                """INSERT INTO jobs
                (id,project_id,milestone_id,job_type,state,priority,correlation_id,attempt_number,
                 max_attempts,scheduled_at,started_at,completed_at,next_retry_at,timeout_seconds,
                 worker_class,payload_json,result_json,last_error_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(job.id),
                    str(job.project_id),
                    str(job.milestone_id) if job.milestone_id else None,
                    job.job_type,
                    job.state.value,
                    job.priority,
                    job.correlation_id,
                    job.attempt_number,
                    job.max_attempts,
                    _ts(job.scheduled_at) if job.scheduled_at else None,
                    _ts(job.started_at) if job.started_at else None,
                    _ts(job.completed_at) if job.completed_at else None,
                    _ts(job.next_retry_at) if job.next_retry_at else None,
                    job.timeout_seconds,
                    job.worker_class.value,
                    _json(job.payload),
                    _json(job.result),
                    job.last_error_id,
                    _ts(job.created_at),
                    _ts(job.updated_at),
                ),
            )
        except sqlite3.IntegrityError as error:
            if job.milestone_id is not None:
                raise JobMilestoneProjectMismatchError(
                    "job milestone does not belong to project"
                ) from error
            raise PersistenceError("job violates persistence integrity") from error

    def get(self, job_id: JobId, project_id: ProjectId | None = None) -> Job:
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE id=?", (str(job_id),)
        ).fetchone()
        if row is None:
            raise PersistenceError("job does not exist")
        if project_id is not None and row["project_id"] != str(project_id):
            raise JobProjectMismatchError("job does not belong to project")
        return Job(
            JobId.from_string(row["id"]),
            ProjectId.from_string(row["project_id"]),
            row["job_type"],
            JobState(row["state"]),
            row["attempt_number"],
            _required_dt(row["created_at"]),
            _required_dt(row["updated_at"]),
            MilestoneId.from_string(row["milestone_id"])
            if row["milestone_id"]
            else None,
            row["priority"],
            row["correlation_id"],
            row["max_attempts"],
            _dt(row["scheduled_at"]),
            _dt(row["started_at"]),
            _dt(row["completed_at"]),
            _dt(row["next_retry_at"]),
            row["timeout_seconds"],
            WorkerClass(row["worker_class"]),
            json.loads(row["payload_json"]),
            json.loads(row["result_json"]),
            row["last_error_id"],
        )

    def attempts(self, job_id: JobId, project_id: ProjectId) -> tuple[JobAttempt, ...]:
        self.get(job_id, project_id)
        rows = self._connection.execute(
            "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number",
            (str(job_id),),
        ).fetchall()
        return tuple(
            JobAttempt(
                job_id,
                r["attempt_number"],
                JobAttemptState(r["state"]),
                _required_dt(r["started_at"]),
                _dt(r["completed_at"]),
                r["external_request_id"],
                r["process_id"],
                r["exit_code"],
                json.loads(r["result_json"]),
                r["error_id"],
                r["logs_reference"],
            )
            for r in rows
        )

    def apply_transition(self, request: JobTransitionRequest) -> Job:
        try:
            with transaction(self._connection):
                row = self._row(request.job_id, request.project_id)
                state = JobState(row["state"])
                if state is not request.expected_state:
                    raise StaleJobStateError("job state changed since it was read")
                if request.occurred_at < _required_dt(row["updated_at"]):
                    raise PersistenceError(
                        "transition timestamp cannot precede job state"
                    )
                validate_job_transition(state, request.target_state)
                self._apply(request, row, state)
                self._history(request, row, state)
        except PersistenceError, ValueError:
            raise
        except sqlite3.Error as error:
            raise PersistenceError("job transition could not be persisted") from error
        return self.get(request.job_id, request.project_id)

    def abandon(self, request: JobTransitionRequest) -> Job:
        if request.target_state is not JobState.ABANDONED:
            raise InvalidAttemptError("abandonment target must be ABANDONED")
        try:
            with transaction(self._connection):
                row = self._row(request.job_id, request.project_id)
                state = JobState(row["state"])
                if state is not request.expected_state:
                    raise StaleJobStateError("job state changed since it was read")
                if request.occurred_at < _required_dt(row["updated_at"]):
                    raise PersistenceError(
                        "abandonment timestamp cannot precede job state"
                    )
                if state in TERMINAL_JOB_STATES:
                    raise TerminalJobMutationError("terminal job cannot be abandoned")
                if row["attempt_number"]:
                    self._finish_attempt(row, JobAttemptState.ABANDONED, request)
                self._update_job(row, request, completed=True)
                self._history(request, row, state)
        except PersistenceError, ValueError:
            raise
        except sqlite3.Error as error:
            raise PersistenceError("job abandonment could not be persisted") from error
        return self.get(request.job_id, request.project_id)

    def _row(self, jid: JobId, pid: ProjectId) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE id=?", (str(jid),)
        ).fetchone()
        if row is None:
            raise PersistenceError("job does not exist")
        if row["project_id"] != str(pid):
            raise JobProjectMismatchError("job does not belong to project")
        return row  # type: ignore[no-any-return]

    def _apply(
        self, r: JobTransitionRequest, row: sqlite3.Row, state: JobState
    ) -> None:
        target = r.target_state
        if target is JobState.RUNNING:
            number = row["attempt_number"] + 1
            if number > row["max_attempts"]:
                raise AttemptLimitExhaustedError("maximum job attempts exhausted")
            self._connection.execute(
                "INSERT INTO job_attempts(id,job_id,attempt_number,state,started_at,result_json) VALUES(?,?,?,?,?,?)",
                (
                    self._id_factory(),
                    row["id"],
                    number,
                    JobAttemptState.RUNNING.value,
                    _ts(r.occurred_at),
                    "{}",
                ),
            )
            self._connection.execute(
                "UPDATE jobs SET state=?,attempt_number=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                (
                    target.value,
                    number,
                    _ts(r.occurred_at),
                    _ts(r.occurred_at),
                    row["id"],
                ),
            )
            return
        if target is JobState.RETRY_WAIT:
            if (
                r.next_retry_at is None
                or r.next_retry_at <= r.occurred_at
                or not r.error_id
            ):
                raise InvalidRetryMetadataError(
                    "retry requires a future UTC time and error reference"
                )
            if row["attempt_number"] >= row["max_attempts"]:
                raise AttemptLimitExhaustedError("retry has no remaining attempt")
            self._finish_attempt(row, JobAttemptState.RETRYABLE_FAILURE, r)
        elif target in TERMINAL_JOB_STATES and state in {
            JobState.RUNNING,
            JobState.WAITING_EXTERNAL,
        }:
            status = {
                JobState.SUCCEEDED: JobAttemptState.SUCCEEDED,
                JobState.FAILED: JobAttemptState.FAILED,
                JobState.CANCELLED: JobAttemptState.CANCELLED,
            }[target]
            self._finish_attempt(row, status, r)
        self._update_job(row, r, completed=target in TERMINAL_JOB_STATES)

    def _finish_attempt(
        self, row: sqlite3.Row, state: JobAttemptState, r: JobTransitionRequest
    ) -> None:
        cursor = self._connection.execute(
            """UPDATE job_attempts SET state=?,completed_at=?,external_request_id=?,process_id=?,exit_code=?,result_json=?,error_id=?,logs_reference=? WHERE job_id=? AND attempt_number=? AND state='RUNNING'""",
            (
                state.value,
                _ts(r.occurred_at),
                r.external_request_id,
                r.process_id,
                r.exit_code,
                _json(r.result),
                r.error_id,
                r.logs_reference,
                row["id"],
                row["attempt_number"],
            ),
        )
        if cursor.rowcount != 1:
            raise ImmutableTerminalAttemptError(
                "current attempt is missing or terminal"
            )

    def _update_job(
        self, row: sqlite3.Row, r: JobTransitionRequest, *, completed: bool
    ) -> None:
        cursor = self._connection.execute(
            """UPDATE jobs SET state=?,completed_at=?,next_retry_at=?,result_json=?,last_error_id=?,updated_at=? WHERE id=? AND state=?""",
            (
                r.target_state.value,
                _ts(r.occurred_at) if completed else row["completed_at"],
                _ts(r.next_retry_at)
                if r.target_state is JobState.RETRY_WAIT and r.next_retry_at
                else None,
                _json(r.result) if r.result is not None else row["result_json"],
                r.error_id or row["last_error_id"],
                _ts(r.occurred_at),
                row["id"],
                r.expected_state.value,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleJobStateError("job state changed while transitioning")

    def _history(
        self, r: JobTransitionRequest, row: sqlite3.Row, state: JobState
    ) -> None:
        self._connection.execute(
            """INSERT INTO state_transitions(id,entity_type,entity_id,project_id,milestone_id,job_id,previous_state,new_state,reason,trigger_event_id,actor_type,actor_id,correlation_id,created_at,metadata_json) VALUES(?,'JOB',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self._id_factory(),
                row["id"],
                row["project_id"],
                row["milestone_id"],
                row["id"],
                state.value,
                r.target_state.value,
                r.reason,
                r.trigger_event_id,
                r.actor_type,
                r.actor_id,
                r.correlation_id,
                _ts(r.occurred_at),
                _json(r.metadata) if r.metadata is not None else None,
            ),
        )

    def transitions(
        self, job_id: JobId, project_id: ProjectId
    ) -> tuple[JobStateTransition, ...]:
        self.get(job_id, project_id)
        rows = self._connection.execute(
            "SELECT * FROM state_transitions WHERE entity_type='JOB' AND job_id=? AND project_id=? ORDER BY created_at,id",
            (str(job_id), str(project_id)),
        ).fetchall()
        return tuple(
            JobStateTransition(
                r["id"],
                job_id,
                project_id,
                MilestoneId.from_string(r["milestone_id"])
                if r["milestone_id"]
                else None,
                JobState(r["previous_state"]),
                JobState(r["new_state"]),
                r["reason"],
                r["actor_type"],
                r["actor_id"],
                r["correlation_id"],
                _required_dt(r["created_at"]),
                r["trigger_event_id"],
                json.loads(r["metadata_json"]) if r["metadata_json"] else None,
            )
            for r in rows
        )
