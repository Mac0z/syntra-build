from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.application.m32_executors import RepositoryProvisioningExecutor
from syntra_build.application.provisioning import (
    AmbiguousGitHubResult,
    AmbiguousPushResult,
    ProvisioningError,
    ProvisioningFailure,
    RemoteRepository,
    RepositoryProvisioningService,
)
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.application.security import SecurityPolicy
from syntra_build.domain import (
    Job,
    JobId,
    JobState,
    MilestoneId,
    ProjectId,
    ProjectState,
    RepositoryVisibility,
    WorkerClass,
)
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.provisioning import RepositoryProvisioningStatus
from syntra_build.domain.security import SecurityEventType, SecuritySeverity
from syntra_build.infrastructure.persistence import (
    SQLiteProjectRepository,
    SQLiteProvisioningRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 9, 29, 12, 30, tzinfo=UTC)
PID = ProjectId(UUID(int=101))
SPEC_ID = "00000000-0000-0000-0000-000000000102"
AGENTS_ID = "00000000-0000-0000-0000-000000000103"
PACKAGE_ID = "00000000-0000-0000-0000-000000000104"
GATE_ID = "00000000-0000-0000-0000-000000000105"
SPEC = "# Approved specification\n"
AGENTS = "# Approved agent rules\n"
SHA = "b" * 40


def _hash(content: str) -> str:
    return sha256(content.encode()).hexdigest()


def _seed(
    path: Path, visibility: RepositoryVisibility = RepositoryVisibility.PUBLIC
) -> sqlite3.Connection:
    connection = open_database(path)
    apply_migrations(connection)
    now = NOW.isoformat()
    connection.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,
         repository_visibility) VALUES (?,?,?,?,?,?,?,?)""",
        (
            str(PID),
            "Executor app",
            "PROVISIONING",
            now,
            now,
            now,
            "executor-app",
            visibility.value,
        ),
    )
    for document_id, kind, content in (
        (SPEC_ID, "SPEC", SPEC),
        (AGENTS_ID, "AGENTS", AGENTS),
    ):
        connection.execute(
            """INSERT INTO project_documents
            (id,project_id,document_type,revision,status,content,content_hash,
             created_at,created_by,approved_at,approved_by)
            VALUES (?,?,?,1,'DRAFT',?,?,?,?,NULL,NULL)""",
            (
                document_id,
                str(PID),
                kind,
                content,
                _hash(content),
                now,
                "architect",
            ),
        )
    connection.execute(
        """INSERT INTO architect_requests
        (id,project_id,request_type,provider,model,reasoning_level,
         request_schema_version,request_payload_json,correlation_id,started_at,
         completed_at,status)
        VALUES ('request',?,'SPECIFICATION_DRAFT','fake','fake','high','1','{}',
                'design-correlation',?,?,'SUCCEEDED')""",
        (str(PID), now, now),
    )
    connection.execute(
        """INSERT INTO human_gates
        (id,project_id,gate_type,state,title,prompt,expected_response_type,
         options_json,created_at,created_by,correlation_id)
        VALUES (?,?,'DESIGN_APPROVAL','RESOLVED','Approve','Approve?',
                'DESIGN_APPROVAL','[]',?,'system','design-correlation')""",
        (GATE_ID, str(PID), now),
    )
    connection.execute(
        """INSERT INTO design_packages
        (id,project_id,architect_request_id,spec_document_id,agents_document_id,
         repository_visibility,design_summary,planned_milestones_json,
         assumptions_json,non_blocking_issues_json,status,approval_gate_id,
         created_at,approved_at,approved_by)
        VALUES (?,?,?,?,?,?,'summary','[]','[]','[]','APPROVED',?,?,?,'human')""",
        (
            PACKAGE_ID,
            str(PID),
            "request",
            SPEC_ID,
            AGENTS_ID,
            visibility.value,
            GATE_ID,
            now,
            now,
        ),
    )
    connection.execute(
        """UPDATE project_documents
        SET status='APPROVED',approved_at=?,approved_by='human'
        WHERE project_id=?""",
        (now, str(PID)),
    )
    connection.commit()
    return connection


def _job(
    *,
    job_type: str = "REPOSITORY_PROVISION",
    worker_class: WorkerClass = WorkerClass.GITHUB,
    milestone_id: MilestoneId | None = None,
) -> Job:
    return Job(
        JobId.generate(),
        PID,
        job_type,
        JobState.RUNNING,
        1,
        NOW,
        NOW,
        milestone_id,
        correlation_id="scheduler-provision-correlation",
        worker_class=worker_class,
    )


@dataclass
class FakeRemoteState:
    visibility: RepositoryVisibility = RepositoryVisibility.PUBLIC
    remote: RemoteRepository | None = None
    main_sha: str | None = None
    contents: dict[str, bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    create_ambiguous: bool = False
    push_ambiguous: bool = False
    remote_document_mismatch: bool = False
    lookup_failure: ProvisioningFailure | None = None


class FakeGitHub:
    def __init__(self, connection: sqlite3.Connection, state: FakeRemoteState) -> None:
        self.connection = connection
        self.state = state

    def get_repository(self, owner: str, name: str) -> RemoteRepository | None:
        self.state.calls.append("get_repository")
        if self.state.lookup_failure is not None:
            raise ProvisioningError(
                self.state.lookup_failure, "synthetic provider failure"
            )
        return self.state.remote

    def create_repository(
        self, name: str, visibility: RepositoryVisibility
    ) -> RemoteRepository:
        self.state.calls.append(f"create_repository:{visibility.value}")
        row = self.connection.execute(
            "SELECT status FROM github_repositories WHERE project_id=?",
            (str(PID),),
        ).fetchone()
        assert row is not None and row["status"] in {"INTENDED", "CREATE_AMBIGUOUS"}
        self.state.remote = RemoteRepository(
            321, "Mac0z", name, f"Mac0z/{name}", visibility, None
        )
        if self.state.create_ambiguous:
            self.state.create_ambiguous = False
            raise AmbiguousGitHubResult("example ambiguous create")
        return self.state.remote

    def configure_repository(self, owner: str, name: str) -> None:
        self.state.calls.append("configure_repository")
        assert self.state.remote is not None
        self.state.remote = RemoteRepository(
            self.state.remote.external_id,
            owner,
            name,
            f"{owner}/{name}",
            self.state.remote.visibility,
            "main",
        )

    def main_sha(self, owner: str, name: str) -> str | None:
        self.state.calls.append("main_sha")
        return self.state.main_sha

    def file_content(
        self, owner: str, name: str, commit_sha: str, path: str
    ) -> bytes | None:
        self.state.calls.append(f"file_content:{path}")
        if self.state.remote_document_mismatch and path == "SPEC.md":
            return b"# Different public example\n"
        return self.state.contents.get(path)


class FakeGit:
    def __init__(self, state: FakeRemoteState) -> None:
        self.state = state

    def create_commit(
        self, project_id: ProjectId, files: dict[str, bytes], message: str
    ) -> str:
        self.state.calls.append("create_commit")
        assert project_id == PID
        assert message == "Initial approved project design"
        assert files == {"SPEC.md": SPEC.encode(), "AGENTS.md": AGENTS.encode()}
        self.state.contents = dict(files)
        return SHA

    def push_main(
        self, project_id: ProjectId, remote_url: str, expected_sha: str
    ) -> None:
        self.state.calls.append("push_main")
        assert project_id == PID
        assert remote_url == "https://github.com/Mac0z/executor-app.git"
        assert expected_sha == SHA
        self.state.main_sha = expected_sha
        if self.state.push_ambiguous:
            raise AmbiguousPushResult("example ambiguous push")


def _executor(
    path: Path,
    state: FakeRemoteState,
    worker_connections: list[sqlite3.Connection],
) -> RepositoryProvisioningExecutor:
    def connection_factory(database_path: Path) -> sqlite3.Connection:
        connection = open_database(database_path)
        worker_connections.append(connection)
        return connection

    def service_factory(
        connection: sqlite3.Connection,
    ) -> RepositoryProvisioningService:
        return RepositoryProvisioningService(
            connection,
            FakeGitHub(connection, state),
            FakeGit(state),
            owner="Mac0z",
            security_policy=SecurityPolicy(connection),
        )

    return RepositoryProvisioningExecutor(
        path,
        service_factory,
        clock=lambda: NOW,
        connection_factory=connection_factory,
    )


@pytest.mark.parametrize(
    "visibility", [RepositoryVisibility.PUBLIC, RepositoryVisibility.PRIVATE]
)
def test_provisions_approved_baseline_on_worker_owned_connection(
    tmp_path: Path, visibility: RepositoryVisibility
) -> None:
    path = tmp_path / "state.db"
    control = _seed(path, visibility)
    state = FakeRemoteState(visibility=visibility)
    worker_connections: list[sqlite3.Connection] = []

    result = _executor(path, state, worker_connections).execute(_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert len(worker_connections) == 1
    assert worker_connections[0] is not control
    assert state.calls.count(f"create_repository:{visibility.value}") == 1
    assert state.calls.count("push_main") == 1
    repository = SQLiteProvisioningRepository(control).for_project(PID)
    baseline = SQLiteProvisioningRepository(control).baseline(PID)
    assert repository is not None
    assert repository.status is RepositoryProvisioningStatus.VERIFIED
    assert repository.external_repository_id == 321
    assert repository.visibility is visibility
    assert baseline is not None
    assert baseline.commit_sha == SHA
    assert baseline.spec_content_hash == _hash(SPEC)
    assert baseline.agents_content_hash == _hash(AGENTS)
    assert baseline.verified_at == NOW
    assert state.contents == {"SPEC.md": SPEC.encode(), "AGENTS.md": AGENTS.encode()}
    project = SQLiteProjectRepository(control, lambda: "unused").get(PID)
    assert project.state is ProjectState.READY
    transition = control.execute(
        """SELECT correlation_id,created_at FROM state_transitions
        WHERE project_id=? ORDER BY created_at DESC LIMIT 1""",
        (str(PID),),
    ).fetchone()
    assert transition["correlation_id"] == "scheduler-provision-correlation"
    assert datetime.fromisoformat(transition["created_at"]) == NOW
    control.close()


@pytest.mark.parametrize(
    ("job_type", "worker_class", "milestone_id"),
    [
        ("SPECIFICATION_DRAFT", WorkerClass.GITHUB, None),
        ("REPOSITORY_PROVISION", WorkerClass.ARCHITECT, None),
        ("REPOSITORY_PROVISION", WorkerClass.GITHUB, MilestoneId.generate()),
    ],
)
def test_rejects_invalid_job_before_opening_resources(
    tmp_path: Path,
    job_type: str,
    worker_class: WorkerClass,
    milestone_id: MilestoneId | None,
) -> None:
    path = tmp_path / "state.db"
    control = _seed(path)
    state = FakeRemoteState()
    worker_connections: list[sqlite3.Connection] = []

    with pytest.raises(ValueError):
        _executor(path, state, worker_connections).execute(
            _job(
                job_type=job_type,
                worker_class=worker_class,
                milestone_id=milestone_id,
            )
        )

    assert worker_connections == []
    assert state.calls == []
    control.close()


def test_collision_is_permanent_and_does_not_claim_remote(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    control = _seed(path)
    state = FakeRemoteState(
        remote=RemoteRepository(
            999,
            "Mac0z",
            "executor-app",
            "Mac0z/executor-app",
            RepositoryVisibility.PUBLIC,
            "main",
        )
    )

    result = _executor(path, state, []).execute(_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.PERMANENT
    assert result.error_id == "repository-provision-collision"
    assert not any(call.startswith("create_repository") for call in state.calls)
    assert "push_main" not in state.calls
    repository = SQLiteProvisioningRepository(control).for_project(PID)
    assert repository is not None and repository.external_repository_id is None
    assert (
        SQLiteProjectRepository(control, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )
    control.close()


@pytest.mark.parametrize(
    ("failure", "classification"),
    [
        (ProvisioningFailure.TRANSIENT, FailureClassification.TRANSIENT),
        (ProvisioningFailure.AMBIGUOUS, FailureClassification.PERMANENT),
    ],
)
def test_maps_only_explicit_transient_failure_as_retryable(
    tmp_path: Path,
    failure: ProvisioningFailure,
    classification: FailureClassification,
) -> None:
    path = tmp_path / "state.db"
    control = _seed(path)
    state = FakeRemoteState(lookup_failure=failure)

    result = _executor(path, state, []).execute(_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is classification
    assert result.next_retry_at is None
    assert SQLiteProjectRepository(control, lambda: "unused").get(PID).state is (
        ProjectState.PROVISIONING
    )
    control.close()


def test_ambiguous_create_is_reconciled_without_duplicate_create(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    control = _seed(path)
    state = FakeRemoteState(create_ambiguous=True)

    result = _executor(path, state, []).execute(_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert sum(call.startswith("create_repository") for call in state.calls) == 1
    assert (
        SQLiteProjectRepository(control, lambda: "unused").get(PID).state
        is ProjectState.READY
    )
    control.close()


def test_ambiguous_push_is_reconciled_without_duplicate_push(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    control = _seed(path)
    state = FakeRemoteState(push_ambiguous=True)

    result = _executor(path, state, []).execute(_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert state.calls.count("push_main") == 1
    assert state.main_sha == SHA
    assert (
        SQLiteProjectRepository(control, lambda: "unused").get(PID).state
        is ProjectState.READY
    )
    control.close()


def test_remote_document_mismatch_fails_without_verification(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    control = _seed(path)
    state = FakeRemoteState(remote_document_mismatch=True)

    result = _executor(path, state, []).execute(_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.error_id == "repository-provision-document_mismatch"
    repository = SQLiteProvisioningRepository(control).for_project(PID)
    baseline = SQLiteProvisioningRepository(control).baseline(PID)
    assert repository is not None and repository.verified_at is None
    assert baseline is not None and baseline.verified_at is None
    assert (
        SQLiteProjectRepository(control, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )
    control.close()


def test_security_block_prevents_privileged_mutation(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    control = _seed(path)
    SecurityPolicy(control, lambda: "blocking-event").record(
        SecurityEventType.REPOSITORY_IDENTITY_MISMATCH,
        SecuritySeverity.HIGH,
        project_id=PID,
        source_component="m32_executor_test",
        source_reference="synthetic-example",
        correlation_id="security-correlation",
        safe_details={"fixture": "non-secret-example"},
        blocking=True,
        created_at=NOW,
    )
    control.commit()
    state = FakeRemoteState()

    result = _executor(path, state, []).execute(_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.error_id == "repository-provision-security_blocked"
    assert result.failure_classification is FailureClassification.PERMANENT
    assert not any(call.startswith("create_repository") for call in state.calls)
    assert "create_commit" not in state.calls
    assert "push_main" not in state.calls
    assert SQLiteProvisioningRepository(control).for_project(PID) is None
    assert (
        SQLiteProjectRepository(control, lambda: "unused").get(PID).state
        is ProjectState.PROVISIONING
    )
    control.close()
