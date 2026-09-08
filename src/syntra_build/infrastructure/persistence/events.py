"""SQLite persistence for immutable workflow events and processing metadata."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime

from syntra_build.domain.events import (
    DuplicateEvent,
    EventInsertionResult,
    EventProcessingStatus,
    InsertedEvent,
    JsonValue,
    PersistedWorkflowEvent,
    WorkflowEvent,
    WorkflowEventSource,
)
from syntra_build.domain.identifiers import (
    GateId,
    JobId,
    MilestoneId,
    ProjectId,
    WorkflowEventId,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import (
    EventAlreadyProcessedError,
    EventClaimConflictError,
    EventNotFoundError,
    EventParentMismatchError,
    InvalidEventCausationError,
    PersistenceError,
)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _json_value(value: object) -> JsonValue:
    """Copy frozen domain JSON into plain JSON-compatible containers."""
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("domain payload contained a non-JSON value")


class SQLiteWorkflowEventRepository:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def add(self, event: WorkflowEvent) -> EventInsertionResult:
        """Insert an event, or deterministically return its external duplicate."""
        if event.external_deduplication_key is not None:
            existing = self.find_by_external_deduplication_key(
                event.external_deduplication_key
            )
            if existing is not None:
                return DuplicateEvent(existing)
        self._validate_relations(event)
        try:
            self._connection.execute(
                """INSERT INTO workflow_events
                (id,event_type,source,correlation_id,causation_event_id,
                 external_deduplication_key,project_id,milestone_id,job_id,gate_id,
                 occurred_at,received_at,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(event.id),
                    event.event_type,
                    event.source.value,
                    event.correlation_id,
                    str(event.causation_event_id) if event.causation_event_id else None,
                    event.external_deduplication_key,
                    str(event.project_id),
                    str(event.milestone_id) if event.milestone_id else None,
                    str(event.job_id) if event.job_id else None,
                    str(event.gate_id) if event.gate_id else None,
                    _timestamp(event.occurred_at),
                    _timestamp(event.received_at),  # type: ignore[arg-type]
                    json.dumps(
                        _json_value(event.payload), sort_keys=True, allow_nan=False
                    ),
                ),
            )
        except sqlite3.IntegrityError as error:
            if event.external_deduplication_key is not None:
                existing = self.find_by_external_deduplication_key(
                    event.external_deduplication_key
                )
                if existing is not None:
                    return DuplicateEvent(existing)
            message = str(error)
            if (
                "causation" in message
                or "workflow_events.project_id, workflow_events.id" in message
            ):
                raise InvalidEventCausationError(
                    "event causation is invalid"
                ) from error
            if "FOREIGN KEY" in message or "inconsistent" in message:
                raise EventParentMismatchError(
                    "event related identity does not belong to project"
                ) from error
            raise PersistenceError(
                "workflow event violates persistence integrity"
            ) from error
        except sqlite3.Error as error:
            raise PersistenceError("workflow event could not be stored") from error
        return InsertedEvent(self.get(event.id))

    def _validate_relations(self, event: WorkflowEvent) -> None:
        project = self._connection.execute(
            "SELECT 1 FROM projects WHERE id=?", (str(event.project_id),)
        ).fetchone()
        if project is None:
            raise EventParentMismatchError("event project does not exist")
        if event.causation_event_id is not None:
            cause = self._connection.execute(
                "SELECT project_id FROM workflow_events WHERE id=?",
                (str(event.causation_event_id),),
            ).fetchone()
            if cause is None or cause["project_id"] != str(event.project_id):
                raise InvalidEventCausationError(
                    "event cause must exist in the same project"
                )
        relations = (
            ("milestones", event.milestone_id, "milestone"),
            ("jobs", event.job_id, "job"),
            ("human_gates", event.gate_id, "gate"),
        )
        for table, identity, label in relations:
            if (
                identity is not None
                and self._connection.execute(
                    f"SELECT 1 FROM {table} WHERE id=? AND project_id=?",
                    (str(identity), str(event.project_id)),
                ).fetchone()
                is None
            ):
                raise EventParentMismatchError(
                    f"event {label} does not belong to project"
                )

    def get(self, event_id: WorkflowEventId) -> PersistedWorkflowEvent:
        row = self._connection.execute(
            "SELECT * FROM workflow_events WHERE id=?", (str(event_id),)
        ).fetchone()
        if row is None:
            raise EventNotFoundError("workflow event does not exist")
        event = WorkflowEvent(
            id=WorkflowEventId.from_string(row["id"]),
            project_id=ProjectId.from_string(row["project_id"]),
            event_type=row["event_type"],
            occurred_at=_datetime(row["occurred_at"]),  # type: ignore[arg-type]
            correlation_id=row["correlation_id"],
            milestone_id=MilestoneId.from_string(row["milestone_id"])
            if row["milestone_id"]
            else None,
            job_id=JobId.from_string(row["job_id"]) if row["job_id"] else None,
            gate_id=GateId.from_string(row["gate_id"]) if row["gate_id"] else None,
            payload=json.loads(row["payload_json"]),
            source=WorkflowEventSource(row["source"]),
            received_at=_datetime(row["received_at"]),
            causation_event_id=WorkflowEventId.from_string(row["causation_event_id"])
            if row["causation_event_id"]
            else None,
            external_deduplication_key=row["external_deduplication_key"],
        )
        return PersistedWorkflowEvent(
            event,
            EventProcessingStatus(row["processing_status"]),
            row["processing_attempt_count"],
            _datetime(row["processed_at"]),
            row["last_processing_error"],
        )

    def find_by_external_deduplication_key(
        self, key: str
    ) -> PersistedWorkflowEvent | None:
        row = self._connection.execute(
            "SELECT id FROM workflow_events WHERE external_deduplication_key=?", (key,)
        ).fetchone()
        return self.get(WorkflowEventId.from_string(row["id"])) if row else None

    def list_pending(self) -> tuple[PersistedWorkflowEvent, ...]:
        return self._list("processing_status='PENDING'")

    def list_failed(self) -> tuple[PersistedWorkflowEvent, ...]:
        return self._list("processing_status='FAILED'")

    def for_project(self, project_id: ProjectId) -> tuple[PersistedWorkflowEvent, ...]:
        return self._list("project_id=?", (str(project_id),))

    def _list(
        self, where: str, parameters: tuple[object, ...] = ()
    ) -> tuple[PersistedWorkflowEvent, ...]:
        rows = self._connection.execute(
            f"SELECT id FROM workflow_events WHERE {where} ORDER BY received_at,id",
            parameters,
        ).fetchall()
        return tuple(self.get(WorkflowEventId.from_string(row["id"])) for row in rows)

    @contextmanager
    def processing_attempt(
        self, event_id: WorkflowEventId, claim_token: str, attempted_at: datetime
    ) -> Iterator[PersistedWorkflowEvent]:
        """Own one atomic event effect transaction until the handler completes."""
        try:
            with transaction(self._connection):
                record = self.get(event_id)
                if record.processing_status in {
                    EventProcessingStatus.PROCESSED,
                    EventProcessingStatus.REJECTED,
                }:
                    raise EventAlreadyProcessedError("workflow event is terminal")
                cursor = self._connection.execute(
                    """UPDATE workflow_events
                       SET processing_claim_token=?,processing_claimed_at=?
                       WHERE id=? AND processing_status IN ('PENDING','FAILED')
                         AND processing_claim_token IS NULL""",
                    (claim_token, _timestamp(attempted_at), str(event_id)),
                )
                if cursor.rowcount != 1:
                    raise EventClaimConflictError("workflow event is already claimed")
                yield record
        except EventAlreadyProcessedError, EventClaimConflictError:
            raise
        except sqlite3.Error as error:
            if "locked" in str(error).casefold():
                raise EventClaimConflictError(
                    "workflow event claim is contended"
                ) from error
            raise PersistenceError(
                "workflow event attempt could not be completed"
            ) from error

    def finish_attempt(
        self,
        event_id: WorkflowEventId,
        claim_token: str,
        status: EventProcessingStatus,
        attempted_at: datetime,
        detail: str | None,
    ) -> None:
        """Finish a claim inside the caller's event-effect transaction."""
        if not self._connection.in_transaction:
            raise PersistenceError("event completion requires an active transaction")
        processed_at = (
            _timestamp(attempted_at)
            if status
            in {EventProcessingStatus.PROCESSED, EventProcessingStatus.REJECTED}
            else None
        )
        cursor = self._connection.execute(
            """UPDATE workflow_events SET processing_status=?,
               processing_attempt_count=processing_attempt_count+1,processed_at=?,
               last_processing_error=CASE
                   WHEN ? IS NULL THEN last_processing_error ELSE ? END,
               processing_claim_token=NULL,processing_claimed_at=NULL
               WHERE id=? AND processing_claim_token=?""",
            (status.value, processed_at, detail, detail, str(event_id), claim_token),
        )
        if cursor.rowcount != 1:
            raise EventClaimConflictError("workflow event claim became stale")

    def record_failed_attempt(
        self, event_id: WorkflowEventId, attempted_at: datetime, detail: str
    ) -> PersistedWorkflowEvent:
        """Persist failure after the atomic effect transaction has rolled back."""
        del attempted_at  # failure has no terminal processed timestamp
        with transaction(self._connection):
            cursor = self._connection.execute(
                """UPDATE workflow_events SET processing_status='FAILED',
                   processing_attempt_count=processing_attempt_count+1,processed_at=NULL,
                   last_processing_error=?,processing_claim_token=NULL,processing_claimed_at=NULL
                   WHERE id=? AND processing_status IN ('PENDING','FAILED')
                     AND processing_claim_token IS NULL""",
                (detail, str(event_id)),
            )
            if cursor.rowcount != 1:
                current = self.get(event_id)
                if current.processing_status in {
                    EventProcessingStatus.PROCESSED,
                    EventProcessingStatus.REJECTED,
                }:
                    raise EventAlreadyProcessedError("workflow event is terminal")
                raise EventClaimConflictError("workflow event failure record is stale")
        return self.get(event_id)
