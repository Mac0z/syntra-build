from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.application.architect import ArchitectError, ArchitectFailureKind
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.application.m32_executors import SpecificationDraftExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.application.specification import SpecificationDraftService
from syntra_build.domain import (
    DocumentType,
    Job,
    JobId,
    JobState,
    MilestoneId,
    PlannedMilestone,
    Project,
    ProjectCreationContext,
    ProjectId,
    ProjectState,
    RepositoryVisibility,
    SpecificationDraft,
    SpecificationDraftRequest,
    WorkerClass,
)
from syntra_build.domain.failures import FailureClassification
from syntra_build.infrastructure.persistence import (
    SQLiteArchitectInteractionRepository,
    SQLiteDesignMessageRepository,
    SQLiteDesignPackageRepository,
    SQLiteHumanGateRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)


class FakeSpecificationProvider:
    provider_name = "fake"
    model = "deterministic"

    def __init__(self, failure: ArchitectError | None = None) -> None:
        self.failure = failure
        self.requests: list[SpecificationDraftRequest] = []

    def draft_specification(
        self, request: SpecificationDraftRequest
    ) -> SpecificationDraft:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return SpecificationDraft(
            request.interface_version,
            request.correlation_id,
            request.project_id,
            "A specification reconstructed from persisted context.",
            request.required_repository_visibility,
            "# SPEC\nBuild the durable project.",
            "# AGENTS\nFollow the project rules.",
            ("Python 3.14",),
            (),
            (PlannedMilestone("M1", "Implement"),),
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": "fake-specification-response"}


def _job(
    project_id: ProjectId,
    *,
    job_type: str = "SPECIFICATION_DRAFT",
    worker_class: WorkerClass = WorkerClass.ARCHITECT,
    milestone_id: MilestoneId | None = None,
) -> Job:
    return Job(
        JobId.generate(),
        project_id,
        job_type,
        JobState.RUNNING,
        1,
        NOW,
        NOW,
        milestone_id,
        correlation_id="scheduler-specification-correlation",
        worker_class=worker_class,
    )


def _seed(path: Path) -> tuple[ProjectId, sqlite3.Connection]:
    control_connection = open_database(path)
    apply_migrations(control_connection)
    project_id = ProjectId.generate()
    projects = SQLiteProjectRepository(control_connection, lambda: str(uuid4()))
    projects.add(
        Project(project_id, "Durable project", ProjectState.DESIGNING, NOW, NOW)
    )
    projects.add_creation_context(
        ProjectCreationContext(
            project_id,
            "owner-id",
            "Build only from this persisted initial request",
            "telegram",
            "conversation-id",
            None,
            "update-id",
            "message-id",
            NOW,
        )
    )
    return project_id, control_connection


def _executor(
    path: Path,
    provider: FakeSpecificationProvider,
    worker_connections: list[sqlite3.Connection],
) -> SpecificationDraftExecutor:
    def connection_factory(database_path: Path) -> sqlite3.Connection:
        connection = open_database(database_path)
        worker_connections.append(connection)
        return connection

    def service_factory(connection: sqlite3.Connection) -> SpecificationDraftService:
        projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
        documents = SQLiteProjectDocumentRepository(connection)
        context = ProjectDesignContextService(
            projects,
            SQLiteDesignMessageRepository(connection),
            SQLiteProjectDecisionRepository(connection),
            documents,
        )
        return SpecificationDraftService(
            context,
            SQLiteArchitectInteractionRepository(connection),
            provider,
            SQLiteDesignPackageRepository(connection),
            documents,
            SQLiteHumanGateRepository(connection, lambda: str(uuid4())),
            projects,
            clock=lambda: NOW,
            id_factory=lambda: "specification-request-id",
        )

    return SpecificationDraftExecutor(path, service_factory, connection_factory)


def test_generates_specification_from_durable_context_on_owned_connection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "syntra.db"
    project_id, control_connection = _seed(path)
    provider = FakeSpecificationProvider()
    worker_connections: list[sqlite3.Connection] = []

    result = _executor(path, provider, worker_connections).execute(_job(project_id))

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert len(worker_connections) == 1
    assert worker_connections[0] is not control_connection
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.project_id == project_id
    assert request.correlation_id == "scheduler-specification-correlation"
    assert request.initial_request == "Build only from this persisted initial request"
    assert request.required_repository_visibility is RepositoryVisibility.PUBLIC

    interaction = control_connection.execute(
        """SELECT q.status,q.correlation_id,s.normalised_payload_json
           FROM architect_requests q
           JOIN architect_responses s ON s.architect_request_id=q.id"""
    ).fetchone()
    assert tuple(interaction[:2]) == ("SUCCEEDED", request.correlation_id)
    response = json.loads(interaction["normalised_payload_json"])
    assert response["project_id"] == str(project_id)
    assert response["repository_visibility"] == "public"

    documents = SQLiteProjectDocumentRepository(control_connection).for_project(
        project_id
    )
    assert {item.document_type: item.revision for item in documents} == {
        DocumentType.SPEC: 1,
        DocumentType.AGENTS: 1,
    }
    package = SQLiteDesignPackageRepository(control_connection).pending_for_project(
        project_id
    )
    assert package is not None
    assert package.repository_visibility is RepositoryVisibility.PUBLIC
    assert (
        control_connection.execute(
            "SELECT count(*) FROM design_packages WHERE project_id=?",
            (str(project_id),),
        ).fetchone()[0]
        == 1
    )
    gate = SQLiteHumanGateRepository(control_connection, lambda: "unused").get(
        package.approval_gate_id
    )
    assert gate.gate_type.value == "DESIGN_APPROVAL"
    assert gate.state.value == "PENDING"
    assert (
        control_connection.execute(
            """SELECT count(*) FROM human_gates
           WHERE project_id=? AND gate_type='DESIGN_APPROVAL'""",
            (str(project_id),),
        ).fetchone()[0]
        == 1
    )
    assert (
        SQLiteProjectRepository(control_connection, lambda: "unused")
        .get(project_id)
        .state
        is ProjectState.DESIGN_APPROVAL
    )
    control_connection.close()


@pytest.mark.parametrize(
    ("job_type", "worker_class", "milestone_id"),
    [
        ("ARCHITECT_DESIGN", WorkerClass.ARCHITECT, None),
        ("SPECIFICATION_DRAFT", WorkerClass.CODEX, None),
        ("SPECIFICATION_DRAFT", WorkerClass.ARCHITECT, MilestoneId.generate()),
    ],
)
def test_rejects_invalid_jobs_before_opening_connection_or_calling_provider(
    tmp_path: Path,
    job_type: str,
    worker_class: WorkerClass,
    milestone_id: MilestoneId | None,
) -> None:
    path = tmp_path / "syntra.db"
    project_id, control_connection = _seed(path)
    provider = FakeSpecificationProvider()
    worker_connections: list[sqlite3.Connection] = []

    with pytest.raises(ValueError):
        _executor(path, provider, worker_connections).execute(
            _job(
                project_id,
                job_type=job_type,
                worker_class=worker_class,
                milestone_id=milestone_id,
            )
        )

    assert worker_connections == []
    assert provider.requests == []
    control_connection.close()


@pytest.mark.parametrize(
    ("failure_kind", "expected_classification"),
    [
        (ArchitectFailureKind.TIMEOUT, FailureClassification.TRANSIENT),
        (ArchitectFailureKind.MALFORMED_RESPONSE, FailureClassification.PERMANENT),
    ],
)
def test_provider_failure_is_persisted_without_partial_design_package(
    tmp_path: Path,
    failure_kind: ArchitectFailureKind,
    expected_classification: FailureClassification,
) -> None:
    path = tmp_path / "syntra.db"
    project_id, control_connection = _seed(path)
    provider = FakeSpecificationProvider(
        ArchitectError(failure_kind, "provider failed")
    )

    result = _executor(path, provider, []).execute(_job(project_id))

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.error_id == f"specification-draft-{failure_kind.value.casefold()}"
    assert result.failure_classification is expected_classification
    request = control_connection.execute(
        "SELECT status,failure_classification FROM architect_requests"
    ).fetchone()
    assert tuple(request) == ("FAILED", failure_kind.value)
    for table in (
        "architect_responses",
        "project_documents",
        "design_packages",
        "human_gates",
    ):
        assert (
            control_connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            == 0
        )
    assert (
        SQLiteProjectRepository(control_connection, lambda: "unused")
        .get(project_id)
        .state
        is ProjectState.DESIGNING
    )
    control_connection.close()
