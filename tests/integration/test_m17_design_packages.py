from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from syntra_build.adapters.architect import SPECIFICATION_DRAFT_SCHEMA
from syntra_build.application.architect import ArchitectError, ArchitectFailureKind
from syntra_build.application.commands import CommandParser, InboundMessage
from syntra_build.application.commands.router import CommandRouter
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.application.gates import HumanGateService
from syntra_build.application.specification import (
    SpecificationDraftService,
    approval_message,
    build_specification_request,
    execute_specification_draft,
)
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
    SpecificationDraftRequest,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.infrastructure.config import SecretInputs, SecretValue, load_config
from syntra_build.infrastructure.persistence import (
    SQLiteArchitectInteractionRepository,
    SQLiteDesignMessageRepository,
    SQLiteDesignPackageRepository,
    SQLiteHumanGateRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    apply_migrations,
    document_content_hash,
    open_database,
)
from syntra_build.smoke import build_host_router

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


@pytest.mark.parametrize(
    "field",
    [
        "interface_version",
        "correlation_id",
        "project_id",
        "design_summary",
        "repository_visibility",
        "spec_markdown",
        "agents_markdown",
    ],
)
def test_draft_rejects_non_string_scalar_fields(field: str) -> None:
    value = draft_data()
    value[field] = 7
    with pytest.raises(DomainValidationError):
        SpecificationDraft.from_dict(value)


@pytest.mark.parametrize("field", ["assumptions", "non_blocking_issues"])
def test_draft_rejects_non_string_collection_members(field: str) -> None:
    value = draft_data()
    value[field] = [7]
    with pytest.raises(DomainValidationError):
        SpecificationDraft.from_dict(value)


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


class FakeDraftProvider:
    provider_name = "openai"
    model = "gpt-5.6-sol"

    def __init__(self) -> None:
        self.requests: list[SpecificationDraftRequest] = []

    def draft_specification(
        self, request: SpecificationDraftRequest
    ) -> SpecificationDraft:
        self.requests.append(request)
        generation = len(self.requests)
        return SpecificationDraft(
            request.interface_version,
            request.correlation_id,
            request.project_id,
            f"Summary {generation}",
            request.required_repository_visibility,
            f"# SPEC {generation}",
            f"# AGENTS {generation}",
            (),
            (),
            (PlannedMilestone("M1", "Build"),),
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": f"response-{len(self.requests)}"}


class FailingDraftProvider(FakeDraftProvider):
    def draft_specification(
        self, request: SpecificationDraftRequest
    ) -> SpecificationDraft:
        raise ArchitectError(ArchitectFailureKind.TIMEOUT, "timed out")


class CapturingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[str] = []
        self.fail = fail

    def send(self, text: str) -> str:
        self.messages.append(text)
        if self.fail:
            raise RuntimeError("notification failed")
        return "telegram-message"


def _draft_service(
    db: sqlite3.Connection, provider: FakeDraftProvider
) -> SpecificationDraftService:
    projects = SQLiteProjectRepository(db, lambda: str(uuid4()))
    documents = SQLiteProjectDocumentRepository(db)
    context = ProjectDesignContextService(
        projects,
        SQLiteDesignMessageRepository(db),
        SQLiteProjectDecisionRepository(db),
        documents,
    )
    return SpecificationDraftService(
        context,
        SQLiteArchitectInteractionRepository(db),
        provider,
        SQLiteDesignPackageRepository(db),
        documents,
        SQLiteHumanGateRepository(db, lambda: str(UUID(int=901))),
        projects,
        clock=lambda: NOW,
    )


def _notify(
    db: sqlite3.Connection, package_id: DesignPackageId, notifier: CapturingNotifier
) -> None:
    packages = SQLiteDesignPackageRepository(db)
    package = packages.get(package_id)
    gates = SQLiteHumanGateRepository(db, lambda: str(uuid4()))
    HumanGateService(
        gates,
        response_id_factory=lambda: str(UUID(int=903)),
        authorised_responder_ids=frozenset({"123"}),
    ).notify(package.approval_gate_id, notifier, occurred_at=NOW)


def _telegram_router(db: sqlite3.Connection) -> CommandRouter:
    config = load_config(
        {"telegram": {"enabled": True, "authorised_user_ids": [123]}},
        environ={},
        secrets=SecretInputs(telegram_bot_token=SecretValue("synthetic-token")),
    )
    return build_host_router(config, db)


def test_real_generation_notification_router_change_and_approval_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "end-to-end.db"
    db = open_database(path)
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "unused").add(
        Project(PID, "Demo", ProjectState.DESIGNING, NOW, NOW)
    )
    provider = FakeDraftProvider()

    first = _draft_service(db, provider).generate(PID, "draft-1")
    documents = SQLiteProjectDocumentRepository(db)
    spec = documents.get(PID, first.spec_document_id)
    agents = documents.get(PID, first.agents_document_id)
    assert (spec.revision, agents.revision) == (1, 1)
    assert (spec.content, agents.content) == ("# SPEC 1", "# AGENTS 1")
    assert (
        SQLiteHumanGateRepository(db, lambda: "unused")
        .get(first.approval_gate_id)
        .state.value
        == "PENDING"
    )
    assert (
        SQLiteProjectRepository(db, lambda: "unused").get(PID).state
        is ProjectState.DESIGN_APPROVAL
    )

    failed = CapturingNotifier(fail=True)
    with pytest.raises(RuntimeError, match="notification failed"):
        _notify(db, first.id, failed)
    assert len(SQLiteProjectDocumentRepository(db).for_project(PID)) == 2
    assert SQLiteDesignPackageRepository(db).pending_for_project(PID) == first

    notifier = CapturingNotifier()
    _notify(db, first.id, notifier)
    notice = notifier.messages[0]
    for expected in (
        "Demo",
        "PUBLIC",
        "SPEC.md: r1",
        "AGENTS.md: r1",
        "Summary 1",
        "Planned milestones: 1",
        str(first.approval_gate_id),
    ):
        assert expected in notice

    db.close()
    db = open_database(path)
    documents = SQLiteProjectDocumentRepository(db)
    response = _telegram_router(db).route(
        InboundMessage(
            "telegram",
            "update-1",
            "message-1",
            "123",
            NOW,
            f"gate {first.approval_gate_id} REQUEST_CHANGES add offline mode",
        )
    )
    assert "REJECTED" in response.text
    assert SQLiteDesignPackageRepository(db).feedback(PID) == ("add offline mode",)
    assert all(
        item.status is DocumentStatus.REJECTED
        for item in SQLiteProjectDocumentRepository(db).for_project(PID)
    )
    assert provider.requests[0].prior_change_feedback == ()

    second = _draft_service(db, provider).generate(PID, "draft-2")
    assert provider.requests[1].prior_change_feedback == ("add offline mode",)
    assert documents.get(PID, second.spec_document_id).revision == 2
    assert documents.get(PID, second.agents_document_id).revision == 2
    _notify(db, second.id, CapturingNotifier())
    approved = _telegram_router(db).route(
        InboundMessage(
            "telegram",
            "update-2",
            "message-2",
            "123",
            NOW,
            f"gate {second.approval_gate_id} APPROVE",
        )
    )
    assert "APPROVED" in approved.text
    assert (
        SQLiteProjectRepository(db, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )
    actors = [
        row[0]
        for row in db.execute(
            """SELECT actor_type FROM state_transitions WHERE gate_id=?
           ORDER BY rowid""",
            (str(second.approval_gate_id),),
        )
    ]
    assert actors == ["SYSTEM", "HUMAN", "SYSTEM", "SYSTEM"]
    duplicate = _telegram_router(db).route(
        InboundMessage(
            "telegram",
            "update-3",
            "message-2",
            "123",
            NOW,
            f"gate {second.approval_gate_id} APPROVE",
        )
    )
    assert "already closed" in duplicate.text


def test_shared_host_draft_execution_audits_known_failure(tmp_path: Path) -> None:
    db = open_database(tmp_path / "failure.db")
    apply_migrations(db)
    projects = SQLiteProjectRepository(db, lambda: "unused")
    projects.add(Project(PID, "Demo", ProjectState.DESIGNING, NOW, NOW))
    context = ProjectDesignContextService(
        projects,
        SQLiteDesignMessageRepository(db),
        SQLiteProjectDecisionRepository(db),
        SQLiteProjectDocumentRepository(db),
    ).reconstruct(PID)
    request = build_specification_request(context, "failed-draft")
    with pytest.raises(ArchitectError) as caught:
        execute_specification_draft(
            SQLiteArchitectInteractionRepository(db),
            FailingDraftProvider(),
            request,
            request_id="failed-request",
            reasoning_effort="high",
            clock=lambda: NOW,
        )
    assert caught.value.kind is ArchitectFailureKind.TIMEOUT
    row = db.execute(
        "SELECT status,failure_classification FROM architect_requests WHERE id=?",
        ("failed-request",),
    ).fetchone()
    assert row[:] == ("FAILED", "TIMEOUT")
