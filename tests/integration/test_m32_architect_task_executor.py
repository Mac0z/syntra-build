# ruff: noqa: E501
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from syntra_build.application.architect import (
    ArchitectError,
    ArchitectFailureKind,
    ArchitectProvider,
)
from syntra_build.application.m32_executors import ArchitectTaskExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain import (
    ARCHITECT_TASK_INTERFACE_VERSION,
    ArchitectTask,
    ArchitectTaskRequest,
    ArchitectTaskType,
    Job,
    JobId,
    JobState,
    Milestone,
    MilestoneId,
    MilestoneState,
    Project,
    ProjectId,
    ProjectState,
    WorkerClass,
)
from syntra_build.domain.failures import FailureClassification
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 9, 29, 14, tzinfo=UTC)
PID = ProjectId(UUID(int=401))
MID = MilestoneId(UUID(int=402))
JID = JobId(UUID(int=403))
SPEC_ID = "00000000-0000-0000-0000-000000000404"
AGENTS_ID = "00000000-0000-0000-0000-000000000405"
PACKAGE_ID = "00000000-0000-0000-0000-000000000406"
GATE_ID = "00000000-0000-0000-0000-000000000407"
REPOSITORY_ID = "repository-record"
BASELINE_ID = "baseline-record"
SPEC = "# Exact approved SPEC\n"
AGENTS = "# Exact approved AGENTS\n"
SHA = "c" * 40


class FakeTaskProvider:
    provider_name = "fake"
    model = "deterministic"

    def __init__(self, failure: ArchitectError | None = None) -> None:
        self.failure = failure
        self.requests: list[ArchitectTaskRequest] = []

    def task(self, request: ArchitectTaskRequest) -> ArchitectTask:
        self.requests.append(request)
        if self.failure:
            raise self.failure
        return ArchitectTask(
            request.interface_version,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.task_type,
            "Implement the approved milestone",
            ("Use persisted requirements",),
            ("Tests pass",),
            ("No workspace preparation",),
            ("Run focused tests",),
            ("Do not run Codex",),
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": "fake-task-response"}


def _job(**changes: object) -> Job:
    job = Job(
        JID,
        PID,
        "ARCHITECT_TASK",
        JobState.RUNNING,
        1,
        NOW,
        NOW,
        MID,
        correlation_id="scheduler-task-correlation",
        max_attempts=3,
        worker_class=WorkerClass.ARCHITECT,
        payload={"task_type": "IMPLEMENT"},
    )
    return replace(job, **changes)  # type: ignore[arg-type]


def _hash(content: str) -> str:
    return sha256(content.encode()).hexdigest()


def _seed(path: Path) -> sqlite3.Connection:
    db = open_database(path)
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "transition").add(
        Project(PID, "Task project", ProjectState.BUILDING, NOW, NOW)
    )
    SQLiteMilestoneRepository(db, lambda: "transition").add(
        Milestone(
            MID,
            PID,
            0,
            "M1",
            "First milestone",
            MilestoneState.PREPARING_TASK,
            NOW,
            NOW,
        )
    )
    SQLiteJobRepository(db, lambda: "transition").add(_job())
    now = NOW.isoformat()
    db.execute(
        """INSERT INTO architect_requests
        (id,project_id,request_type,provider,model,reasoning_level,
         request_schema_version,request_payload_json,correlation_id,started_at,
         completed_at,status)
        VALUES ('planning-request',?,'SPECIFICATION_DRAFT','fake','fake','high','1','{}',
                'planning',?,?,'SUCCEEDED')""",
        (str(PID), now, now),
    )
    db.execute(
        """INSERT INTO human_gates
        (id,project_id,gate_type,state,title,prompt,expected_response_type,
         options_json,created_at,created_by,correlation_id)
        VALUES (?,?,'DESIGN_APPROVAL','RESOLVED','Approved','Approved?',
                'DESIGN_APPROVAL','[]',?,'system','planning')""",
        (GATE_ID, str(PID), now),
    )
    for document_id, kind, content in (
        (SPEC_ID, "SPEC", SPEC),
        (AGENTS_ID, "AGENTS", AGENTS),
    ):
        db.execute(
            """INSERT INTO project_documents
            (id,project_id,document_type,revision,status,content,content_hash,
             created_at,created_by,approved_at,approved_by)
            VALUES (?,?,?,7,'DRAFT',?,?,?,?,NULL,NULL)""",
            (document_id, str(PID), kind, content, _hash(content), now, "architect"),
        )
    db.execute(
        """INSERT INTO design_packages
        (id,project_id,architect_request_id,spec_document_id,agents_document_id,
         repository_visibility,design_summary,planned_milestones_json,
         assumptions_json,non_blocking_issues_json,status,approval_gate_id,
         created_at,approved_at,approved_by)
        VALUES (?,?,?,?,?,'public','approved plan',?,'[]','[]','PENDING_APPROVAL',?,?,NULL,NULL)""",
        (
            PACKAGE_ID,
            str(PID),
            "planning-request",
            SPEC_ID,
            AGENTS_ID,
            '[{"code":"M1","title":"First milestone"}]',
            GATE_ID,
            now,
        ),
    )
    db.execute(
        """UPDATE project_documents SET status='APPROVED',approved_at=?,approved_by='human'
           WHERE project_id=?""",
        (now, str(PID)),
    )
    db.execute(
        """UPDATE design_packages SET status='APPROVED',approved_at=?,approved_by='human'
           WHERE id=?""",
        (now, PACKAGE_ID),
    )
    db.execute(
        """INSERT INTO github_repositories
        (id,project_id,provider,owner,repository_name,full_name,
         external_repository_id,visibility,default_branch,status,
         created_at,updated_at,verified_at)
        VALUES (?,?, 'github','Mac0z','generated-app','Mac0z/generated-app',321,
                'public','main','VERIFIED',?,?,?)""",
        (REPOSITORY_ID, str(PID), now, now, now),
    )
    db.execute(
        """INSERT INTO repository_baselines
        (id,project_id,github_repository_id,commit_sha,spec_document_id,
         spec_revision,spec_content_hash,agents_document_id,agents_revision,
         agents_content_hash,created_at,verified_at)
        VALUES (?,?,?,?,?,7,?,?,7,?,?,?)""",
        (
            BASELINE_ID,
            str(PID),
            REPOSITORY_ID,
            SHA,
            SPEC_ID,
            _hash(SPEC),
            AGENTS_ID,
            _hash(AGENTS),
            now,
            now,
        ),
    )
    db.commit()
    return db


def _executor(
    path: Path,
    provider: FakeTaskProvider,
    worker_connections: list[sqlite3.Connection] | None = None,
) -> ArchitectTaskExecutor:
    def connections(database_path: Path) -> sqlite3.Connection:
        connection = open_database(database_path)
        if worker_connections is not None:
            worker_connections.append(connection)
        return connection

    return ArchitectTaskExecutor(
        path,
        cast(ArchitectProvider, provider),
        clock=lambda: NOW,
        connection_factory=connections,
    )


def test_reconstructs_accepted_task_and_advances_milestone(tmp_path: Path) -> None:
    path = tmp_path / "syntra.db"
    control = _seed(path)
    provider = FakeTaskProvider()
    workers: list[sqlite3.Connection] = []

    result = _executor(path, provider, workers).execute(_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert len(workers) == 1 and workers[0] is not control
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.interface_version == ARCHITECT_TASK_INTERFACE_VERSION
    assert (request.job_id, request.correlation_id) == (
        JID,
        "scheduler-task-correlation",
    )
    assert (request.project_id, request.milestone_id) == (PID, MID)
    assert request.task_type is ArchitectTaskType.IMPLEMENT
    assert (request.spec_revision, request.spec_content) == ("7", SPEC)
    assert (request.agents_revision, request.agents_content) == ("7", AGENTS)
    assert request.milestone_definition == {
        "id": str(MID),
        "code": "M1",
        "title": "First milestone",
        "sequence_number": 0,
        "state": "PREPARING_TASK",
    }
    assert request.repository_context == {
        "provider": "github",
        "owner": "Mac0z",
        "repository_name": "generated-app",
        "full_name": "Mac0z/generated-app",
        "default_branch": "main",
        "visibility": "public",
        "external_repository_id": 321,
        "provisioning_status": "VERIFIED",
        "baseline_commit_sha": SHA,
    }
    assert request.previous_milestone_summaries == ()
    assert request.failure_evidence is None
    persisted = control.execute(
        "SELECT status,job_id FROM architect_requests WHERE request_type='TASK'"
    ).fetchone()
    assert tuple(persisted) == ("SUCCEEDED", str(JID))
    assert (
        control.execute(
            "SELECT count(*) FROM architect_responses WHERE response_type='TASK'"
        ).fetchone()[0]
        == 1
    )
    assert (
        SQLiteMilestoneRepository(control, lambda: "unused").get(MID, PID).state
        is MilestoneState.PREPARING_WORKSPACE
    )
    transition = control.execute(
        "SELECT correlation_id,new_state FROM state_transitions WHERE entity_type='MILESTONE' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert tuple(transition) == ("scheduler-task-correlation", "PREPARING_WORKSPACE")
    control.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"job_type": "ARCHITECT_DESIGN"},
        {"worker_class": WorkerClass.CODEX},
        {"milestone_id": None},
        {"payload": {}},
        {"payload": {"task_type": "CI_REWORK"}},
        {"payload": {"task_type": "IMPLEMENT", "instructions": "untrusted"}},
    ],
)
def test_rejects_invalid_job_before_opening_resources(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    provider = FakeTaskProvider()
    opened: list[sqlite3.Connection] = []
    with pytest.raises(ValueError):
        _executor(tmp_path / "absent.db", provider, opened).execute(_job(**changes))
    assert opened == []
    assert provider.requests == []


def test_rejects_cross_project_and_wrong_state_before_provider(tmp_path: Path) -> None:
    path = tmp_path / "syntra.db"
    control = _seed(path)
    provider = FakeTaskProvider()
    other = ProjectId.generate()
    SQLiteProjectRepository(control, lambda: "unused").add(
        Project(other, "Other", ProjectState.BUILDING, NOW, NOW)
    )
    control.commit()
    with pytest.raises(Exception, match="milestone does not belong to project"):
        _executor(path, provider).execute(replace(_job(), project_id=other))
    control.execute("UPDATE milestones SET state='CODING' WHERE id=?", (str(MID),))
    control.commit()
    with pytest.raises(ValueError, match="PREPARING_TASK"):
        _executor(path, provider).execute(_job())
    assert provider.requests == []
    control.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM design_packages",
        "UPDATE design_packages SET status='REJECTED',rejected_at='2026-09-29T14:00:00+00:00',approved_at=NULL,approved_by=NULL",
        "UPDATE project_documents SET status='DRAFT',approved_at=NULL,approved_by=NULL WHERE document_type='SPEC'",
        "UPDATE github_repositories SET status='IDENTIFIED',verified_at=NULL",
        "UPDATE repository_baselines SET verified_at=NULL",
    ],
)
def test_missing_or_inconsistent_authoritative_evidence_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "syntra.db"
    control = _seed(path)
    provider = FakeTaskProvider()
    control.execute(mutation)
    control.commit()
    with pytest.raises((ValueError, Exception)):
        _executor(path, provider).execute(_job())
    assert provider.requests == []
    control.close()


@pytest.mark.parametrize(
    ("kind", "classification"),
    [
        (ArchitectFailureKind.TIMEOUT, FailureClassification.TRANSIENT),
        (ArchitectFailureKind.MALFORMED_RESPONSE, FailureClassification.PERMANENT),
    ],
)
def test_persists_architect_failure_without_advancing(
    tmp_path: Path,
    kind: ArchitectFailureKind,
    classification: FailureClassification,
) -> None:
    path = tmp_path / "syntra.db"
    control = _seed(path)
    provider = FakeTaskProvider(ArchitectError(kind, "synthetic failure"))
    result = _executor(path, provider).execute(_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is classification
    assert control.execute(
        "SELECT status,failure_classification FROM architect_requests WHERE request_type='TASK'"
    ).fetchone()[:] == ("FAILED", kind.value)
    assert (
        control.execute(
            "SELECT count(*) FROM architect_responses WHERE response_type='TASK'"
        ).fetchone()[0]
        == 0
    )
    assert (
        SQLiteMilestoneRepository(control, lambda: "unused").get(MID, PID).state
        is MilestoneState.PREPARING_TASK
    )
    control.close()


def test_replay_uses_exact_job_task_without_second_provider_call(
    tmp_path: Path,
) -> None:
    path = tmp_path / "syntra.db"
    control = _seed(path)
    provider = FakeTaskProvider()
    executor = _executor(path, provider)
    assert executor.execute(_job()).disposition is JobExecutionDisposition.SUCCEEDED
    assert executor.execute(_job()).disposition is JobExecutionDisposition.SUCCEEDED
    assert len(provider.requests) == 1
    assert (
        control.execute(
            "SELECT count(*) FROM architect_requests WHERE request_type='TASK'"
        ).fetchone()[0]
        == 1
    )
    assert (
        control.execute(
            "SELECT count(*) FROM architect_responses WHERE response_type='TASK'"
        ).fetchone()[0]
        == 1
    )
    control.close()
