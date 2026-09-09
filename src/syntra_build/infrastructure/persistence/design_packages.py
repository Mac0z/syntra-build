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
    PlannedMilestone,
    ProjectDocumentId,
    ProjectId,
    RepositoryVisibility,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError


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
        """Atomically resolve gate, documents, package, visibility and project state."""
        if outcome not in {"APPROVE", "REQUEST_CHANGES"}:
            raise PersistenceError("unsupported design decision")
        if outcome == "REQUEST_CHANGES" and (feedback is None or not feedback.strip()):
            raise PersistenceError("REQUEST_CHANGES requires feedback")
        hook = failure_hook or (lambda _: None)
        now = occurred_at.isoformat(timespec="microseconds")
        try:
            self.connection.execute("BEGIN")
            package = self.get(package_id)
            gate = self.connection.execute(
                "SELECT * FROM human_gates WHERE id=?", (str(gate_id),)
            ).fetchone()
            project = self.connection.execute(
                "SELECT state FROM projects WHERE id=?", (str(project_id),)
            ).fetchone()
            if (
                package.project_id != project_id
                or package.approval_gate_id != gate_id
                or package.status is not DesignPackageStatus.PENDING_APPROVAL
            ):
                raise PersistenceError(
                    "gate does not identify the pending design package"
                )
            if (
                gate is None
                or gate["project_id"] != str(project_id)
                or gate["gate_type"] != "DESIGN_APPROVAL"
                or gate["state"] != "NOTIFIED"
                or gate["artifact_reference"] != f"design-package:{package_id}"
            ):
                raise PersistenceError(
                    "design approval gate is not answerable for package"
                )
            if project is None or project["state"] != "DESIGN_APPROVAL":
                raise PersistenceError("project is not awaiting design approval")
            documents = {
                row["id"]: row
                for row in self.connection.execute(
                    "SELECT * FROM project_documents WHERE id IN (?,?)",
                    (str(package.spec_document_id), str(package.agents_document_id)),
                )
            }
            spec, agents = (
                documents.get(str(package.spec_document_id)),
                documents.get(str(package.agents_document_id)),
            )
            if (
                spec is None
                or agents is None
                or spec["project_id"] != str(project_id)
                or agents["project_id"] != str(project_id)
                or spec["document_type"] != "SPEC"
                or agents["document_type"] != "AGENTS"
                or spec["status"] != "DRAFT"
                or agents["status"] != "DRAFT"
            ):
                raise PersistenceError("package document revisions are invalid")
            self.connection.execute(
                "INSERT INTO human_gate_responses (id,gate_id,message_id,response_code,response_text,selected_option,attachments_json,responded_by,responded_at,validated,validation_notes) VALUES (?,?,?,?,?,?,'[]',?,?,1,'validated by M17 design package policy')",
                (
                    str(uuid4()),
                    str(gate_id),
                    message_id,
                    outcome,
                    feedback,
                    outcome,
                    responder,
                    now,
                ),
            )
            if outcome == "APPROVE":
                for label, document in (("spec", spec), ("agents", agents)):
                    prior = self.connection.execute(
                        "SELECT id FROM project_documents WHERE project_id=? AND document_type=? AND status='APPROVED'",
                        (str(project_id), document["document_type"]),
                    ).fetchone()
                    self.connection.execute(
                        "UPDATE project_documents SET status='SUPERSEDED' WHERE project_id=? AND document_type=? AND status='APPROVED'",
                        (str(project_id), document["document_type"]),
                    )
                    self.connection.execute(
                        "UPDATE project_documents SET status='APPROVED',approved_at=?,approved_by=?,supersedes_document_id=? WHERE id=? AND status='DRAFT'",
                        (
                            now,
                            responder,
                            prior["id"] if prior else None,
                            document["id"],
                        ),
                    )
                    hook(label)
                self.connection.execute(
                    "UPDATE design_packages SET status='APPROVED',approved_at=?,approved_by=? WHERE id=?",
                    (now, responder, str(package_id)),
                )
                hook("package")
                target = "PROVISIONING"
                self.connection.execute(
                    "UPDATE projects SET repository_visibility=?,state=?,updated_at=?,last_state_change_at=? WHERE id=? AND state='DESIGN_APPROVAL'",
                    (
                        package.repository_visibility.value,
                        target,
                        now,
                        now,
                        str(project_id),
                    ),
                )
            else:
                self.connection.execute(
                    "UPDATE project_documents SET status='REJECTED' WHERE id IN (?,?) AND status='DRAFT'",
                    (str(package.spec_document_id), str(package.agents_document_id)),
                )
                self.connection.execute(
                    "UPDATE design_packages SET status='REJECTED',rejected_at=?,rejection_feedback=? WHERE id=?",
                    (now, feedback, str(package_id)),
                )
                self.connection.execute(
                    "INSERT INTO design_change_feedback (id,package_id,project_id,feedback,provided_by,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        str(package_id),
                        str(project_id),
                        feedback,
                        responder,
                        now,
                    ),
                )
                target = "DESIGNING"
                self.connection.execute(
                    "UPDATE projects SET state=?,updated_at=?,last_state_change_at=? WHERE id=? AND state='DESIGN_APPROVAL'",
                    (target, now, now, str(project_id)),
                )
            hook("project")
            self.connection.execute(
                "UPDATE human_gates SET state='RESOLVED',responded_at=?,resolved_at=? WHERE id=? AND state='NOTIFIED'",
                (now, now, str(gate_id)),
            )
            for previous, new, reason in (
                ("NOTIFIED", "RESPONDED", "human response recorded"),
                ("RESPONDED", "VALIDATED", "design decision validated"),
                ("VALIDATED", "RESOLVED", "design decision applied"),
            ):
                self.connection.execute(
                    """INSERT INTO state_transitions
                    (id,entity_type,entity_id,project_id,gate_id,previous_state,new_state,reason,
                     actor_type,actor_id,correlation_id,created_at)
                    VALUES (?,'HUMAN_GATE',?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid4()),
                        str(gate_id),
                        str(project_id),
                        str(gate_id),
                        previous,
                        new,
                        reason,
                        "HUMAN",
                        responder,
                        correlation_id,
                        now,
                    ),
                )
            hook("gate")
            self.connection.execute(
                "INSERT INTO state_transitions (id,entity_type,entity_id,project_id,previous_state,new_state,reason,actor_type,actor_id,correlation_id,created_at) VALUES (?,'PROJECT',?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    str(project_id),
                    str(project_id),
                    "DESIGN_APPROVAL",
                    target,
                    "human design decision",
                    "HUMAN",
                    responder,
                    correlation_id,
                    now,
                ),
            )
            self.connection.commit()
        except BaseException:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        return self.get(package_id)
