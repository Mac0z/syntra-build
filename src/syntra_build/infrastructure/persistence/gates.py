# ruff: noqa: E501
"""Transactional SQLite persistence for human gates and response evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from syntra_build.domain.gate_state_machine import (
    GateTransitionRequest,
    validate_gate_transition,
)
from syntra_build.domain.gates import (
    ExpectedResponseType,
    GateState,
    GateType,
    HumanGate,
    HumanGateResponse,
)
from syntra_build.domain.identifiers import GateId, MilestoneId, ProjectId
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.projects import ProjectState
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import (
    DuplicateGateResponseError,
    GateMilestoneProjectMismatchError,
    GateNotFoundError,
    GateProjectMismatchError,
    PersistenceError,
    StaleGateStateError,
)


def _ts(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _required_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class SQLiteHumanGateRepository:
    def __init__(self, connection: sqlite3.Connection, id_factory: Callable[[], str]):
        self._connection, self._id_factory = connection, id_factory

    def add(self, gate: HumanGate) -> None:
        try:
            self._connection.execute(
                """INSERT INTO human_gates
              (id,project_id,milestone_id,gate_type,state,title,prompt,expected_response_type,
               options_json,architect_recommendation,resume_project_state,resume_milestone_state,
               created_at,notified_at,responded_at,resolved_at,created_by,correlation_id,artifact_reference)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(gate.id),
                    str(gate.project_id),
                    str(gate.milestone_id) if gate.milestone_id else None,
                    gate.gate_type.value,
                    gate.state.value,
                    gate.title,
                    gate.prompt,
                    gate.expected_response_type.value,
                    json.dumps(gate.options),
                    gate.architect_recommendation,
                    gate.resume_project_state.value
                    if gate.resume_project_state
                    else None,
                    gate.resume_milestone_state.value
                    if gate.resume_milestone_state
                    else None,
                    _ts(gate.created_at),
                    _ts(gate.notified_at) if gate.notified_at else None,
                    _ts(gate.responded_at) if gate.responded_at else None,
                    _ts(gate.resolved_at) if gate.resolved_at else None,
                    gate.created_by,
                    gate.correlation_id,
                    gate.artifact_reference,
                ),
            )
        except sqlite3.IntegrityError as error:
            message = str(error)
            if "milestone" in message or "FOREIGN KEY" in message:
                raise GateMilestoneProjectMismatchError(
                    "gate milestone does not belong to project"
                ) from error
            raise PersistenceError("human gate could not be stored") from error

    def get(self, gate_id: GateId) -> HumanGate:
        row = self._connection.execute(
            "SELECT * FROM human_gates WHERE id=?", (str(gate_id),)
        ).fetchone()
        if row is None:
            raise GateNotFoundError("human gate does not exist")
        return HumanGate(
            id=GateId.from_string(row["id"]),
            project_id=ProjectId.from_string(row["project_id"]),
            milestone_id=MilestoneId.from_string(row["milestone_id"])
            if row["milestone_id"]
            else None,
            gate_type=GateType(row["gate_type"]),
            state=GateState(row["state"]),
            requested_at=_required_dt(row["created_at"]),
            title=row["title"],
            prompt=row["prompt"],
            expected_response_type=ExpectedResponseType(row["expected_response_type"]),
            options=tuple(json.loads(row["options_json"])),
            architect_recommendation=row["architect_recommendation"],
            resume_project_state=ProjectState(row["resume_project_state"])
            if row["resume_project_state"]
            else None,
            resume_milestone_state=MilestoneState(row["resume_milestone_state"])
            if row["resume_milestone_state"]
            else None,
            notified_at=_dt(row["notified_at"]),
            responded_at=_dt(row["responded_at"]),
            resolved_at=_dt(row["resolved_at"]),
            created_by=row["created_by"],
            correlation_id=row["correlation_id"],
            artifact_reference=row["artifact_reference"],
        )

    def apply_transition(self, request: GateTransitionRequest) -> HumanGate:
        with transaction(self._connection):
            self._transition(request)
        return self.get(request.gate_id)

    def _transition(self, request: GateTransitionRequest) -> None:
        row = self._connection.execute(
            "SELECT state,project_id,milestone_id,created_at FROM human_gates WHERE id=?",
            (str(request.gate_id),),
        ).fetchone()
        if row is None:
            raise GateNotFoundError("human gate does not exist")
        if row["project_id"] != str(request.project_id):
            raise GateProjectMismatchError("gate belongs to another project")
        persisted = GateState(row["state"])
        if persisted is not request.expected_state:
            raise StaleGateStateError("gate state changed since it was read")
        validate_gate_transition(persisted, request.target_state)
        field = {
            GateState.NOTIFIED: "notified_at",
            GateState.RESPONDED: "responded_at",
            GateState.RESOLVED: "resolved_at",
        }.get(request.target_state)
        assignment = "state=?" + (f",{field}=?" if field else "")
        values: list[object] = [request.target_state.value]
        if field:
            values.append(_ts(request.occurred_at))
        values.extend((str(request.gate_id), persisted.value))
        if (
            self._connection.execute(
                f"UPDATE human_gates SET {assignment} WHERE id=? AND state=?", values
            ).rowcount
            != 1
        ):
            raise StaleGateStateError("gate changed while transitioning")
        self._connection.execute(
            """INSERT INTO state_transitions
          (id,entity_type,entity_id,project_id,milestone_id,gate_id,previous_state,new_state,reason,
           trigger_event_id,actor_type,actor_id,correlation_id,created_at) VALUES (?,'HUMAN_GATE',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self._id_factory(),
                str(request.gate_id),
                str(request.project_id),
                row["milestone_id"],
                str(request.gate_id),
                persisted.value,
                request.target_state.value,
                request.reason,
                request.trigger_event_id,
                request.actor_type,
                request.actor_id,
                request.correlation_id,
                _ts(request.occurred_at),
            ),
        )

    def record_response(
        self, request: GateTransitionRequest, response: HumanGateResponse
    ) -> HumanGate:
        if (
            request.target_state is not GateState.RESPONDED
            or request.expected_state is not GateState.NOTIFIED
        ):
            raise StaleGateStateError("responses require NOTIFIED to RESPONDED")
        try:
            with transaction(self._connection):
                self._connection.execute(
                    """INSERT INTO human_gate_responses
                 (id,gate_id,message_id,response_code,response_text,selected_option,attachments_json,
                  responded_by,responded_at,validated,validation_notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        response.id,
                        str(response.gate_id),
                        response.message_id,
                        response.response_code,
                        response.response_text,
                        response.selected_option,
                        json.dumps(response.attachments),
                        response.responded_by,
                        _ts(response.responded_at),
                        int(response.validated),
                        response.validation_notes,
                    ),
                )
                self._transition(request)
        except sqlite3.IntegrityError as error:
            if "message_id" in str(error):
                raise DuplicateGateResponseError(
                    "response message was already recorded"
                ) from error
            raise PersistenceError("gate response could not be stored") from error
        return self.get(request.gate_id)

    def responses(self, gate_id: GateId) -> tuple[HumanGateResponse, ...]:
        rows = self._connection.execute(
            "SELECT * FROM human_gate_responses WHERE gate_id=? ORDER BY responded_at,id",
            (str(gate_id),),
        ).fetchall()
        return tuple(
            HumanGateResponse(
                id=r["id"],
                gate_id=gate_id,
                message_id=r["message_id"],
                response_code=r["response_code"],
                response_text=r["response_text"],
                selected_option=r["selected_option"],
                attachments=tuple(json.loads(r["attachments_json"])),
                responded_by=r["responded_by"],
                responded_at=_required_dt(r["responded_at"]),
                validated=bool(r["validated"]),
                validation_notes=r["validation_notes"],
            )
            for r in rows
        )

    def outstanding(self, project_id: ProjectId | None = None) -> tuple[HumanGate, ...]:
        sql = "SELECT id FROM human_gates WHERE state NOT IN ('RESOLVED','CANCELLED')"
        args: tuple[object, ...] = ()
        if project_id:
            sql += " AND project_id=?"
            args = (str(project_id),)
        sql += " ORDER BY created_at,id"
        return tuple(
            self.get(GateId.from_string(r[0]))
            for r in self._connection.execute(sql, args).fetchall()
        )

    def resolution_would_orphan_waiting_project(self, gate: HumanGate) -> bool:
        """Protect WAITING_HUMAN until an atomic resume coordinator is supplied."""
        project = self._connection.execute(
            "SELECT state FROM projects WHERE id=?", (str(gate.project_id),)
        ).fetchone()
        if project is None:
            raise GateNotFoundError("gate project does not exist")
        if project["state"] != "WAITING_HUMAN":
            return False
        row = self._connection.execute(
            """SELECT count(*) FROM human_gates
               WHERE project_id=? AND state NOT IN ('RESOLVED','CANCELLED')""",
            (str(gate.project_id),),
        ).fetchone()
        return int(row[0]) <= 1
