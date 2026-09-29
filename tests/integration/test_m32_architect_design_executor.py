from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Never
from uuid import uuid4

import pytest

from syntra_build.application.architect import (
    ArchitectDesignService,
    ArchitectError,
    ArchitectFailureKind,
)
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.application.m32_executors import ArchitectDesignExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain import (
    ARCHITECT_INTERFACE_VERSION,
    ArchitectDesignMode,
    ArchitectDesignRequest,
    ArchitectDesignResponse,
    ArchitectReviewRequest,
    ArchitectTaskRequest,
    Job,
    JobId,
    JobState,
    MilestoneId,
    Project,
    ProjectCreationContext,
    ProjectId,
    ProjectState,
    SpecificationDraftRequest,
    WorkerClass,
)
from syntra_build.infrastructure.persistence import (
    SQLiteArchitectInteractionRepository,
    SQLiteDesignMessageRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)


class FakeArchitectProvider:
    provider_name = "fake"
    model = "deterministic"

    def __init__(self, failure: ArchitectError | None = None) -> None:
        self.failure = failure
        self.requests: list[ArchitectDesignRequest] = []

    def design(self, request: ArchitectDesignRequest) -> ArchitectDesignResponse:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return ArchitectDesignResponse(
            ARCHITECT_INTERFACE_VERSION,
            request.correlation_id,
            request.project_id,
            ArchitectDesignMode.PROPOSE_DESIGN,
            "Use the persisted requirements.",
            (),
            (),
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": "fake-response"}

    def draft_specification(self, request: SpecificationDraftRequest) -> Never:
        raise AssertionError(f"unexpected specification request: {request}")

    def review(self, request: ArchitectReviewRequest) -> Never:
        raise AssertionError(f"unexpected review request: {request}")

    def task(self, request: ArchitectTaskRequest) -> Never:
        raise AssertionError(f"unexpected task request: {request}")


def _job(
    project_id: ProjectId,
    *,
    job_type: str = "ARCHITECT_DESIGN",
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
        correlation_id="scheduler-correlation",
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
            "Build from durable context only",
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
    provider: FakeArchitectProvider,
    worker_connections: list[sqlite3.Connection],
) -> ArchitectDesignExecutor:
    def connection_factory(database_path: Path) -> sqlite3.Connection:
        connection = open_database(database_path)
        worker_connections.append(connection)
        return connection

    def service_factory(connection: sqlite3.Connection) -> ArchitectDesignService:
        context = ProjectDesignContextService(
            SQLiteProjectRepository(connection, lambda: str(uuid4())),
            SQLiteDesignMessageRepository(connection),
            SQLiteProjectDecisionRepository(connection),
            SQLiteProjectDocumentRepository(connection),
        )
        return ArchitectDesignService(
            context,
            SQLiteArchitectInteractionRepository(connection),
            provider,
            id_factory=lambda: "architect-request-id",
        )

    return ArchitectDesignExecutor(path, service_factory, connection_factory)


def test_executes_design_from_durable_context_on_owned_connection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "syntra.db"
    project_id, control_connection = _seed(path)
    provider = FakeArchitectProvider()
    worker_connections: list[sqlite3.Connection] = []

    result = _executor(path, provider, worker_connections).execute(_job(project_id))

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert len(worker_connections) == 1
    assert worker_connections[0] is not control_connection
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.project_id == project_id
    assert request.correlation_id == "scheduler-correlation"
    assert request.initial_request == "Build from durable context only"
    row = control_connection.execute(
        """SELECT q.status,q.correlation_id,s.normalised_payload_json
           FROM architect_requests q
           JOIN architect_responses s ON s.architect_request_id=q.id"""
    ).fetchone()
    assert row["status"] == "SUCCEEDED"
    assert row["correlation_id"] == "scheduler-correlation"
    assert json.loads(row["normalised_payload_json"])["project_id"] == str(project_id)
    control_connection.close()


@pytest.mark.parametrize(
    ("job_type", "worker_class", "milestone_id"),
    [
        ("ARCHITECT_TASK", WorkerClass.ARCHITECT, None),
        ("ARCHITECT_DESIGN", WorkerClass.CODEX, None),
        ("ARCHITECT_DESIGN", WorkerClass.ARCHITECT, MilestoneId.generate()),
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
    provider = FakeArchitectProvider()
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


def test_provider_failure_is_persisted_and_returns_failed(tmp_path: Path) -> None:
    path = tmp_path / "syntra.db"
    project_id, control_connection = _seed(path)
    provider = FakeArchitectProvider(
        ArchitectError(ArchitectFailureKind.TRANSIENT_PROVIDER, "provider unavailable")
    )

    result = _executor(path, provider, []).execute(_job(project_id))

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is not None
    row = control_connection.execute(
        "SELECT status,failure_classification FROM architect_requests"
    ).fetchone()
    assert tuple(row) == ("FAILED", "TRANSIENT_PROVIDER")
    assert (
        control_connection.execute("SELECT 1 FROM architect_responses").fetchone()
        is None
    )
    control_connection.close()
