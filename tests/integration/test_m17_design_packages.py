from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.adapters.architect import SPECIFICATION_DRAFT_SCHEMA
from syntra_build.application.commands import CommandParser, InboundMessage
from syntra_build.application.specification import approval_message
from syntra_build.domain import (
    DesignPackageId,
    DesignPackageStatus,
    DocumentStatus,
    DocumentType,
    GateId,
    PlannedMilestone,
    Project,
    ProjectDocumentId,
    ProjectId,
    ProjectState,
    RepositoryVisibility,
    SpecificationDraft,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.infrastructure.persistence import (
    SQLiteDesignPackageRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    apply_migrations,
    document_content_hash,
    open_database,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)
PID = ProjectId(UUID(int=1))


def draft_data() -> dict[str, object]:
    return {
        "interface_version": "1.0",
        "correlation_id": "corr",
        "project_id": str(PID),
        "design_summary": "A bounded design",
        "repository_visibility": "private",
        "spec_markdown": "# SPEC\nBuild it.",
        "agents_markdown": "# AGENTS\nTest it.",
        "assumptions": ["Python 3.14"],
        "non_blocking_issues": [],
        "planned_milestones": [{"code": "M0", "title": "Bootstrap"}],
    }


def test_strict_draft_contract_and_nested_schema() -> None:
    draft = SpecificationDraft.from_dict(draft_data())
    assert draft.to_dict() == draft_data()
    malformed = draft_data() | {"extra": True}
    with pytest.raises(DomainValidationError):
        SpecificationDraft.from_dict(malformed)
    malformed = draft_data()
    malformed["planned_milestones"] = [{"code": "M0", "title": "x", "extra": 1}]
    with pytest.raises(DomainValidationError):
        SpecificationDraft.from_dict(malformed)
    assert SPECIFICATION_DRAFT_SCHEMA["additionalProperties"] is False
    properties = SPECIFICATION_DRAFT_SCHEMA["properties"]
    assert isinstance(properties, dict)
    milestones = properties["planned_milestones"]
    assert isinstance(milestones, dict)
    items = milestones["items"]
    assert isinstance(items, dict)
    assert items["additionalProperties"] is False


@pytest.mark.parametrize("field", ["spec_markdown", "agents_markdown"])
def test_draft_rejects_blank_documents(field: str) -> None:
    value = draft_data()
    value[field] = "  "
    with pytest.raises(DomainValidationError):
        SpecificationDraft.from_dict(value)


def setup_pending(
    path: Path,
) -> tuple[sqlite3.Connection, SQLiteDesignPackageRepository, DesignPackageId, GateId]:
    db = open_database(path)
    apply_migrations(db)
    projects = SQLiteProjectRepository(db, lambda: "transition-id")
    projects.add(Project(PID, "Demo", ProjectState.DESIGN_APPROVAL, NOW, NOW))
    db.execute(
        """INSERT INTO architect_requests
        (id,project_id,request_type,provider,model,reasoning_level,request_schema_version,
         request_payload_json,correlation_id,started_at,status)
        VALUES ('request',?,'SPECIFICATION_DRAFT','openai','gpt-5.6-sol','high',
        '1.0','{}','corr',?,'SUCCEEDED')""",
        (str(PID), NOW.isoformat()),
    )
    ids = iter((ProjectDocumentId(UUID(int=2)), ProjectDocumentId(UUID(int=3))))
    documents = SQLiteProjectDocumentRepository(db, lambda: next(ids))
    spec = documents.create_revision(PID, DocumentType.SPEC, "# SPEC", NOW, "ARCHITECT")
    agents = documents.create_revision(
        PID, DocumentType.AGENTS, "# AGENTS", NOW, "ARCHITECT"
    )
    package_id, gate_id = DesignPackageId(UUID(int=4)), GateId(UUID(int=5))
    db.execute(
        """INSERT INTO human_gates
        (id,project_id,gate_type,state,title,prompt,expected_response_type,options_json,
         created_at,notified_at,created_by,correlation_id,artifact_reference)
        VALUES (?,?,'DESIGN_APPROVAL','NOTIFIED','Approve','Review','DESIGN_APPROVAL',
        '["APPROVE","REQUEST_CHANGES"]',?,?,'SYSTEM','corr',?)""",
        (
            str(gate_id),
            str(PID),
            NOW.isoformat(),
            NOW.isoformat(),
            f"design-package:{package_id}",
        ),
    )
    packages = SQLiteDesignPackageRepository(db)
    packages.create(
        project_id=PID,
        architect_request_id="request",
        spec_document_id=spec.id,
        agents_document_id=agents.id,
        visibility=RepositoryVisibility.PRIVATE,
        summary="Summary",
        milestones=(PlannedMilestone("M0", "First"),),
        assumptions=(),
        issues=(),
        gate_id=gate_id,
        created_at=NOW,
        package_id=package_id,
    )
    return db, packages, package_id, gate_id


def test_atomic_approval_establishes_exact_baseline_and_visibility(
    tmp_path: Path,
) -> None:
    db, packages, package_id, gate_id = setup_pending(tmp_path / "m17.db")
    package = packages.decide(
        package_id=package_id,
        gate_id=gate_id,
        project_id=PID,
        outcome="APPROVE",
        feedback=None,
        responder="human",
        message_id="message",
        occurred_at=NOW,
        correlation_id="corr",
    )
    assert package.status is DesignPackageStatus.APPROVED
    assert db.execute("SELECT state,repository_visibility FROM projects").fetchone()[
        :
    ] == ("PROVISIONING", "private")
    rows = db.execute(
        "SELECT document_type,status,content_hash FROM project_documents "
        "ORDER BY document_type"
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("AGENTS", "APPROVED"),
        ("SPEC", "APPROVED"),
    ]
    assert {row[2] for row in rows} == {
        document_content_hash("# SPEC"),
        document_content_hash("# AGENTS"),
    }
    assert db.execute("SELECT state FROM human_gates").fetchone()[0] == "RESOLVED"
    db.close()
    reopened = open_database(tmp_path / "m17.db")
    assert (
        SQLiteDesignPackageRepository(reopened).get(package_id).status
        is DesignPackageStatus.APPROVED
    )


@pytest.mark.parametrize("point", ["spec", "agents", "package", "project", "gate"])
def test_approval_rolls_back_at_every_injected_boundary(
    tmp_path: Path, point: str
) -> None:
    db, packages, package_id, gate_id = setup_pending(tmp_path / f"{point}.db")

    def fail(label: str) -> None:
        if label == point:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        packages.decide(
            package_id=package_id,
            gate_id=gate_id,
            project_id=PID,
            outcome="APPROVE",
            feedback=None,
            responder="human",
            message_id="message",
            occurred_at=NOW,
            correlation_id="corr",
            failure_hook=fail,
        )
    assert db.execute("SELECT state,repository_visibility FROM projects").fetchone()[
        :
    ] == ("DESIGN_APPROVAL", "public")
    assert {row[0] for row in db.execute("SELECT status FROM project_documents")} == {
        "DRAFT"
    }
    assert (
        db.execute("SELECT status FROM design_packages").fetchone()[0]
        == "PENDING_APPROVAL"
    )
    assert db.execute("SELECT state FROM human_gates").fetchone()[0] == "NOTIFIED"


def test_request_changes_preserves_documents_and_feedback(tmp_path: Path) -> None:
    db, packages, package_id, gate_id = setup_pending(tmp_path / "changes.db")
    package = packages.decide(
        package_id=package_id,
        gate_id=gate_id,
        project_id=PID,
        outcome="REQUEST_CHANGES",
        feedback="Add an offline mode",
        responder="human",
        message_id="message",
        occurred_at=NOW,
        correlation_id="corr",
    )
    assert package.status is DesignPackageStatus.REJECTED
    assert packages.feedback(PID) == ("Add an offline mode",)
    assert {row[0] for row in db.execute("SELECT status FROM project_documents")} == {
        DocumentStatus.REJECTED.value
    }
    assert db.execute("SELECT state,repository_visibility FROM projects").fetchone()[
        :
    ] == ("DESIGNING", "public")


def test_notification_and_command_feedback() -> None:
    package = DesignPackageStatus  # keep import assertion explicit
    del package
    data = draft_data()
    assert json.loads(json.dumps(data)) == data
    message = InboundMessage(
        "telegram",
        "u",
        "m",
        "human",
        NOW,
        "gate 00000000-0000-0000-0000-000000000005 REQUEST_CHANGES add offline mode",
    )
    command = CommandParser(lambda: "corr").parse(message).command
    assert command is not None and command.gate_feedback == "add offline mode"


def test_approval_message_contains_human_context(tmp_path: Path) -> None:
    _, packages, package_id, _ = setup_pending(tmp_path / "message.db")
    text = approval_message("Demo", packages.get(package_id), 2, 3)
    for expected in (
        "Demo",
        "PRIVATE",
        "SPEC.md: r2",
        "AGENTS.md: r3",
        "Summary",
        "Planned milestones: 1",
        "APPROVE",
        "REQUEST_CHANGES",
    ):
        assert expected in text
