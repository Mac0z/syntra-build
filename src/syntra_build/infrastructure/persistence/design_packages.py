# ruff: noqa: E501
"""Durable M17 design packages and one-transaction baseline decisions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from syntra_build.domain import (
    DesignPackage,
    DesignPackageId,
    DesignPackageStatus,
    GateId,
    GateState,
    HumanGateResponse,
    PlannedMilestone,
    ProjectDocumentId,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    RepositoryVisibility,
)
from syntra_build.domain.gate_state_machine import GateTransitionRequest
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.design import (
    SQLiteProjectDocumentRepository,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.gates import SQLiteHumanGateRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


class SQLiteDesignPackageRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        id_factory: Callable[[], DesignPackageId] = DesignPackageId.generate,
    ) -> None:
        self.connection, self._ids = connection, id_factory

    def create(
        self,
        *,
        project_id: ProjectId,
        architect_request_id: str,
        spec_document_id: ProjectDocumentId,
        agents_document_id: ProjectDocumentId,
        visibility: RepositoryVisibility,
        summary: str,
        milestones: tuple[PlannedMilestone, ...],
        assumptions: tuple[str, ...],
        issues: tuple[str, ...],
        gate_id: GateId,
        created_at: datetime,
        package_id: DesignPackageId | None = None,
    ) -> DesignPackage:
        package_id = package_id or self._ids()
        try:
            self.connection.execute(
                """INSERT INTO design_packages (id,project_id,architect_request_id,spec_document_id,
                agents_document_id,repository_visibility,design_summary,planned_milestones_json,
                assumptions_json,non_blocking_issues_json,status,approval_gate_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING_APPROVAL',?,?)""",
                (
                    str(package_id),
                    str(project_id),
                    architect_request_id,
                    str(spec_document_id),
                    str(agents_document_id),
                    visibility.value,
                    summary,
                    json.dumps([item.to_dict() for item in milestones]),
                    json.dumps(assumptions),
                    json.dumps(issues),
                    str(gate_id),
                    created_at.isoformat(timespec="microseconds"),
                ),
            )
        except sqlite3.Error as error:
            raise PersistenceError("design package could not be stored") from error
        return self.get(package_id)

    def get(self, package_id: DesignPackageId) -> DesignPackage:
        row = self.connection.execute(
            "SELECT * FROM design_packages WHERE id=?", (str(package_id),)
        ).fetchone()
        if row is None:
            raise PersistenceError("design package does not exist")
        return DesignPackage(
            package_id,
            ProjectId.from_string(row["project_id"]),
            row["architect_request_id"],
            ProjectDocumentId.from_string(row["spec_document_id"]),
            ProjectDocumentId.from_string(row["agents_document_id"]),
            RepositoryVisibility(row["repository_visibility"]),
            row["design_summary"],
            tuple(
                PlannedMilestone.from_dict(x)
                for x in json.loads(row["planned_milestones_json"])
            ),
            tuple(json.loads(row["assumptions_json"])),
            tuple(json.loads(row["non_blocking_issues_json"])),
            DesignPackageStatus(row["status"]),
            GateId.from_string(row["approval_gate_id"]),
            datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            _dt(row["approved_at"]),
            row["approved_by"],
            _dt(row["rejected_at"]),
        )

    def feedback(self, project_id: ProjectId) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self.connection.execute(
                "SELECT feedback FROM design_change_feedback WHERE project_id=? ORDER BY created_at,id",
                (str(project_id),),
            )
        )

    def pending_for_project(self, project_id: ProjectId) -> DesignPackage | None:
        row = self.connection.execute(
            """SELECT id FROM design_packages
               WHERE project_id=? AND status='PENDING_APPROVAL'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (str(project_id),),
        ).fetchone()
        return (
            self.get(DesignPackageId.from_string(row["id"]))
            if row is not None
            else None
        )

    def decide(
        self,
        *,
        package_id: DesignPackageId,
        gate_id: GateId,
        project_id: ProjectId,
        outcome: str,
        feedback: str | None,
        responder: str,
        message_id: str,
        occurred_at: datetime,
        correlation_id: str,
        failure_hook: Callable[[str], None] | None = None,
    ) -> DesignPackage:
        """Apply one exact human decision using existing guarded repositories."""
        if outcome not in {"APPROVE", "REQUEST_CHANGES"}:
            raise PersistenceError("unsupported design decision")
        if outcome == "REQUEST_CHANGES" and (feedback is None or not feedback.strip()):
            raise PersistenceError("REQUEST_CHANGES requires feedback")
        hook = failure_hook or (lambda _: None)
        now = occurred_at.isoformat(timespec="microseconds")
        gates = SQLiteHumanGateRepository(self.connection, lambda: str(uuid4()))
        documents = SQLiteProjectDocumentRepository(self.connection)
        projects = SQLiteProjectRepository(self.connection, lambda: str(uuid4()))
        package = self.get(package_id)
        gate = gates.get(gate_id)
        project = projects.get(project_id)
        if (
            package.project_id != project_id
            or package.approval_gate_id != gate_id
            or package.status is not DesignPackageStatus.PENDING_APPROVAL
        ):
            raise PersistenceError("gate does not identify the pending design package")
        if (
            gate.project_id != project_id
            or gate.gate_type.value != "DESIGN_APPROVAL"
            or gate.state is not GateState.NOTIFIED
            or gate.artifact_reference != f"design-package:{package_id}"
        ):
            raise PersistenceError("design approval gate is not answerable for package")
        if project.state is not ProjectState.DESIGN_APPROVAL:
            raise PersistenceError("project is not awaiting design approval")
        spec = documents.get(project_id, package.spec_document_id)
        agents = documents.get(project_id, package.agents_document_id)
        if (
            spec.document_type.value != "SPEC"
            or agents.document_type.value != "AGENTS"
            or spec.status.value != "DRAFT"
            or agents.status.value != "DRAFT"
        ):
            raise PersistenceError("package document revisions are invalid")
        response = HumanGateResponse(
            str(uuid4()),
            gate_id,
            message_id,
            outcome,
            feedback,
            None,
            (),
            responder,
            occurred_at,
            True,
            "validated by M17 design package policy",
        )
        with transaction(self.connection):
            gate = gates.record_response(
                self._gate_transition(
                    gate_id,
                    project_id,
                    GateState.NOTIFIED,
                    GateState.RESPONDED,
                    "human response received",
                    "HUMAN",
                    responder,
                    correlation_id,
                    occurred_at,
                    message_id,
                ),
                response,
            )
            if outcome == "APPROVE":
                documents.approve(
                    project_id, package.spec_document_id, occurred_at, responder
                )
                hook("spec")
                documents.approve(
                    project_id, package.agents_document_id, occurred_at, responder
                )
                hook("agents")
                projects.set_repository_visibility(
                    project_id, package.repository_visibility, occurred_at
                )
                self.connection.execute(
                    """UPDATE design_packages SET status='APPROVED',approved_at=?,approved_by=?
                       WHERE id=? AND status='PENDING_APPROVAL'""",
                    (now, responder, str(package_id)),
                )
                target = ProjectState.PROVISIONING
            else:
                documents.reject(project_id, package.spec_document_id)
                documents.reject(project_id, package.agents_document_id)
                self.connection.execute(
                    """UPDATE design_packages SET status='REJECTED',rejected_at=?,rejection_feedback=?
                       WHERE id=? AND status='PENDING_APPROVAL'""",
                    (now, feedback, str(package_id)),
                )
                self.connection.execute(
                    """INSERT INTO design_change_feedback
                       (id,package_id,project_id,feedback,provided_by,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        str(uuid4()),
                        str(package_id),
                        str(project_id),
                        feedback,
                        responder,
                        now,
                    ),
                )
                target = ProjectState.DESIGNING
            hook("package")
            projects.apply_transition(
                ProjectTransitionRequest(
                    project_id,
                    ProjectState.DESIGN_APPROVAL,
                    target,
                    "human design decision",
                    "HUMAN",
                    responder,
                    correlation_id,
                    occurred_at,
                    message_id,
                )
            )
            hook("project")
            gate = gates.apply_transition(
                self._gate_transition(
                    gate_id,
                    project_id,
                    GateState.RESPONDED,
                    GateState.VALIDATED,
                    "response schema validated",
                    "SYSTEM",
                    None,
                    correlation_id,
                    occurred_at,
                    message_id,
                )
            )
            gates.apply_transition(
                self._gate_transition(
                    gate_id,
                    project_id,
                    GateState.VALIDATED,
                    GateState.RESOLVED,
                    "validated response resolved gate",
                    "SYSTEM",
                    None,
                    correlation_id,
                    occurred_at,
                    message_id,
                )
            )
            hook("gate")
        return self.get(package_id)

    @staticmethod
    def _gate_transition(
        gate_id: GateId,
        project_id: ProjectId,
        previous: GateState,
        target: GateState,
        reason: str,
        actor_type: str,
        actor_id: str | None,
        correlation_id: str,
        occurred_at: datetime,
        trigger: str,
    ) -> GateTransitionRequest:
        return GateTransitionRequest(
            gate_id,
            project_id,
            previous,
            target,
            reason,
            actor_type,
            actor_id,
            correlation_id,
            occurred_at,
            None,
            trigger,
        )
