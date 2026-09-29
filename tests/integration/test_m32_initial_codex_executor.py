# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from syntra_build.application.m32_executors import InitialCodexExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain import (
    ARCHITECT_TASK_INTERFACE_VERSION,
    ArchitectTask,
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
from syntra_build.domain.codex import (
    CodexProcessStatus,
    CodexRunRequest,
    CodexRunResult,
)
from syntra_build.domain.failures import FailureClassification
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)
from syntra_build.infrastructure.persistence.codex import SQLiteCodexRunRepository

NOW = datetime(2026, 9, 29, 18, tzinfo=UTC)
PID = ProjectId(UUID(int=601))
MID = MilestoneId(UUID(int=602))
JID = JobId(UUID(int=603))
WORKTREE_ID = "00000000-0000-0000-0000-000000000604"
SPEC_ID = "00000000-0000-0000-0000-000000000605"
AGENTS_ID = "00000000-0000-0000-0000-000000000606"
PACKAGE_ID = "00000000-0000-0000-0000-000000000607"
GATE_ID = "00000000-0000-0000-0000-000000000608"
HEAD = "a" * 40
AGENTS = "# Approved engineering instructions"


def job(**changes: object) -> Job:
    value = Job(
        JID,
        PID,
        "CODEX_RUN",
        JobState.RUNNING,
        0,
        NOW,
        NOW,
        MID,
        correlation_id="scheduler-correlation",
        max_attempts=3,
        worker_class=WorkerClass.CODEX,
        payload={},
    )
    return replace(value, **changes)  # type: ignore[arg-type]


def seed(
    path: Path,
    worktree: Path,
    *,
    task: bool = True,
    agents_status: str = "APPROVED",
    workspace_state: str = "READY",
) -> sqlite3.Connection:
    db = open_database(path)
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "transition").add(
        Project(PID, "Codex project", ProjectState.BUILDING, NOW, NOW)
    )
    SQLiteMilestoneRepository(db, lambda: "transition").add(
        Milestone(MID, PID, 0, "M1", "Implement", MilestoneState.CODING, NOW, NOW)
    )
    SQLiteJobRepository(db, lambda: "transition").add(job())
    stamp = NOW.isoformat()
    db.execute(
        "INSERT INTO architect_requests (id,project_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status) VALUES ('design',?,'DESIGN','fake','fake','high','1.0','{}','design',?,?,'SUCCEEDED')",
        (str(PID), stamp, stamp),
    )
    db.execute(
        "INSERT INTO human_gates (id,project_id,gate_type,state,title,prompt,expected_response_type,options_json,created_at,created_by,correlation_id) VALUES (?,?,'DESIGN_APPROVAL','RESOLVED','Approved','Approve?','DESIGN_APPROVAL','[]',?,'human','design')",
        (GATE_ID, str(PID), stamp),
    )
    for document_id, kind, content in (
        (SPEC_ID, "SPEC", "# Spec"),
        (AGENTS_ID, "AGENTS", AGENTS),
    ):
        db.execute(
            "INSERT INTO project_documents (id,project_id,document_type,revision,status,content,content_hash,created_at,created_by) VALUES (?,?,?,1,'DRAFT',?,?,?,'architect')",
            (
                document_id,
                str(PID),
                kind,
                content,
                hashlib.sha256(content.encode()).hexdigest(),
                stamp,
            ),
        )
    db.execute(
        "INSERT INTO design_packages (id,project_id,architect_request_id,spec_document_id,agents_document_id,repository_visibility,design_summary,planned_milestones_json,assumptions_json,non_blocking_issues_json,status,approval_gate_id,created_at) VALUES (?,?,?,?,?,'public','plan','[]','[]','[]','PENDING_APPROVAL',?,?)",
        (PACKAGE_ID, str(PID), "design", SPEC_ID, AGENTS_ID, GATE_ID, stamp),
    )
    db.execute(
        "UPDATE project_documents SET status='APPROVED',approved_at=?,approved_by='human'",
        (stamp,),
    )
    db.execute(
        "UPDATE design_packages SET status='APPROVED',approved_at=?,approved_by='human' WHERE id=?",
        (stamp, PACKAGE_ID),
    )
    if agents_status != "APPROVED":
        db.execute(
            "UPDATE project_documents SET status=?,approved_at=NULL,approved_by=NULL WHERE id=?",
            (agents_status, AGENTS_ID),
        )
    if task:
        accepted = ArchitectTask(
            ARCHITECT_TASK_INTERFACE_VERSION,
            "architect-correlation",
            PID,
            MID,
            ArchitectTaskType.IMPLEMENT,
            "Implement exactly",
            ("requirement",),
            ("criterion",),
            (),
            ("pytest",),
            (),
        )
        db.execute(
            "INSERT INTO architect_requests (id,project_id,milestone_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status) VALUES ('task',?,?,'TASK','fake','fake','high','1.0','{}','architect-correlation',?,?,'SUCCEEDED')",
            (str(PID), str(MID), stamp, stamp),
        )
        db.execute(
            "INSERT INTO architect_responses (id,architect_request_id,response_type,response_schema_version,normalised_payload_json,status,created_at,validation_status,provider,model) VALUES ('task-result','task','TASK','1.0',?,'ACCEPTED',?,'VALID','fake','fake')",
            (json.dumps(accepted.to_dict()), stamp),
        )
    db.execute(
        "INSERT INTO github_repositories (id,project_id,provider,owner,repository_name,full_name,external_repository_id,visibility,default_branch,status,created_at,updated_at,verified_at) VALUES ('gh',?,'github','Mac0z','app','Mac0z/app',1,'public','main','VERIFIED',?,?,?)",
        (str(PID), stamp, stamp, stamp),
    )
    db.execute(
        "INSERT INTO git_repositories (id,project_id,github_repository_id,repository_path,remote_name,remote_url,default_branch,created_at,updated_at) VALUES ('repo',?,'gh',?,'origin','https://github.com/Mac0z/app.git','main',?,?)",
        (str(PID), str(worktree.parent / "repo.git"), stamp, stamp),
    )
    worktree.mkdir(parents=True)
    db.execute(
        "INSERT INTO git_workspaces (id,project_id,milestone_id,git_repository_id,branch_name,worktree_path,base_branch,base_sha,current_head_sha,state,created_at,last_validated_at) VALUES (?,?,?,'repo','syntra/m00-implement',?,'main',?,?,?, ?,?)",
        (
            WORKTREE_ID,
            str(PID),
            str(MID),
            str(worktree),
            HEAD,
            HEAD,
            workspace_state,
            stamp,
            stamp,
        ),
    )
    db.commit()
    return db


class RecordingRunner:
    def __init__(
        self,
        connection: sqlite3.Connection,
        status: CodexProcessStatus = CodexProcessStatus.SUCCEEDED,
        *,
        mismatch: bool = False,
    ) -> None:
        self.connection = connection
        self.status = status
        self.mismatch = mismatch
        self.requests: list[CodexRunRequest] = []
        self.validations: list[bool] = []

    def validate_workspace(
        self, request: CodexRunRequest, *, require_clean: bool
    ) -> None:
        self.validations.append(require_clean)
        row = self.connection.execute(
            "SELECT state,worktree_path FROM git_workspaces WHERE project_id=? AND milestone_id=?",
            (str(request.project_id), str(request.milestone_id)),
        ).fetchone()
        if (
            row is None
            or Path(row["worktree_path"]) != request.worktree_path
            or (require_clean and row["state"] != "READY")
        ):
            raise ValueError("workspace is not authoritative")

    def run(self, request: CodexRunRequest) -> CodexRunResult:
        self.requests.append(request)
        records = SQLiteCodexRunRepository(self.connection)
        records.start(
            f"run-{request.attempt_number}",
            request,
            WORKTREE_ID,
            "f" * 64,
            NOW,
            "stdout",
            "stderr",
        )
        result = CodexRunResult(
            request.interface_version,
            "wrong" if self.mismatch else request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.job_id,
            request.attempt_number,
            self.status,
            NOW,
            NOW,
            "fake-codex",
            request.timeout_seconds,
            exit_code=0 if self.status is CodexProcessStatus.SUCCEEDED else 1,
            stdout_reference="stdout",
            stderr_reference="stderr",
        )
        records.complete(f"run-{request.attempt_number}", result)
        return result

    def cancel(self, job_id: str, attempt_number: int) -> bool:
        return False


def executor(
    path: Path,
    runners: list[RecordingRunner],
    *,
    status: CodexProcessStatus = CodexProcessStatus.SUCCEEDED,
    mismatch: bool = False,
    connections: list[sqlite3.Connection] | None = None,
) -> InitialCodexExecutor:
    def connection_factory(database_path: Path) -> sqlite3.Connection:
        connection = open_database(database_path)
        if connections is not None:
            connections.append(connection)
        return connection

    def runner_factory(connection: sqlite3.Connection) -> RecordingRunner:
        runner = RecordingRunner(connection, status, mismatch=mismatch)
        runners.append(runner)
        return runner

    return InitialCodexExecutor(
        path,
        runner_factory,
        timeout_seconds=123,
        clock=lambda: NOW,
        connection_factory=connection_factory,
    )


def state(db: sqlite3.Connection) -> str:
    return cast(
        str,
        db.execute("SELECT state FROM milestones WHERE id=?", (str(MID),)).fetchone()[
            0
        ],
    )


def test_success_uses_durable_inputs_worker_connection_and_advances(
    tmp_path: Path,
) -> None:
    path, worktree = tmp_path / "state.db", tmp_path / "worktree"
    control = seed(path, worktree)
    runners: list[RecordingRunner] = []
    workers: list[sqlite3.Connection] = []
    result = executor(path, runners, connections=workers).execute(job())
    request = runners[0].requests[0]
    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert (
        request.interface_version,
        request.correlation_id,
        request.project_id,
        request.milestone_id,
        request.job_id,
        request.attempt_number,
    ) == ("1.0", "scheduler-correlation", PID, MID, JID, 1)
    assert request.worktree_path == worktree and request.agents_markdown == AGENTS
    assert (
        request.task["task_type"] == "IMPLEMENT"
        and request.task["objective"] == "Implement exactly"
    )
    assert request.timeout_seconds == 123
    assert (
        len(workers) == 1
        and runners[0].connection is workers[0]
        and workers[0] is not control
    )
    row = control.execute(
        "SELECT project_id,milestone_id,job_id,attempt_number,process_status,correlation_id FROM codex_runs"
    ).fetchone()
    assert tuple(row) == (
        str(PID),
        str(MID),
        str(JID),
        1,
        "SUCCEEDED",
        "scheduler-correlation",
    )
    assert state(control) == "VALIDATING_CHANGES"


@pytest.mark.parametrize(
    "changes",
    [
        {"job_type": "CHANGE_VALIDATE"},
        {"worker_class": WorkerClass.GIT},
        {"milestone_id": None},
        {"payload": {"task": "untrusted"}},
    ],
)
def test_invalid_job_is_rejected_before_resources(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    opened = False

    def connections(_path: Path) -> sqlite3.Connection:
        nonlocal opened
        opened = True
        raise AssertionError

    instance = InitialCodexExecutor(
        tmp_path / "missing.db",
        lambda _db: pytest.fail("runner created"),
        timeout_seconds=1,
        connection_factory=connections,
    )
    with pytest.raises(ValueError):
        instance.execute(job(**changes))
    assert not opened


@pytest.mark.parametrize(
    "seed_changes",
    [{"task": False}, {"agents_status": "REJECTED"}, {"workspace_state": "ERROR"}],
)
def test_missing_authoritative_evidence_fails_before_codex(
    tmp_path: Path, seed_changes: dict[str, object]
) -> None:
    path = tmp_path / "state.db"
    seed(path, tmp_path / "worktree", **seed_changes)  # type: ignore[arg-type]
    runners: list[RecordingRunner] = []
    with pytest.raises(Exception):
        executor(path, runners).execute(job())
    assert not runners or not runners[0].requests


def test_result_identity_mismatch_does_not_advance(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    control = seed(path, tmp_path / "worktree")
    with pytest.raises(ValueError, match="result identity"):
        executor(path, [], mismatch=True).execute(job())
    assert state(control) == "CODING"


@pytest.mark.parametrize(
    "status,classification",
    [
        (CodexProcessStatus.FAILED, FailureClassification.PERMANENT),
        (CodexProcessStatus.TIMED_OUT, FailureClassification.TRANSIENT),
        (CodexProcessStatus.CANCELLED, FailureClassification.CANCELLED),
        (CodexProcessStatus.ABANDONED, FailureClassification.PERMANENT),
    ],
)
def test_unsuccessful_outcomes_are_durable_and_do_not_advance(
    tmp_path: Path, status: CodexProcessStatus, classification: FailureClassification
) -> None:
    path = tmp_path / "state.db"
    control = seed(path, tmp_path / "worktree")
    result = executor(path, [], status=status).execute(job())
    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is classification
    assert (
        control.execute("SELECT process_status FROM codex_runs").fetchone()[0]
        == status.value
    )
    assert state(control) == "CODING"


def persist_success(db: sqlite3.Connection, request: CodexRunRequest) -> None:
    records = SQLiteCodexRunRepository(db)
    records.start("replay", request, WORKTREE_ID, "f" * 64, NOW, "stdout", "stderr")
    records.complete(
        "replay",
        CodexRunResult(
            "1.0",
            request.correlation_id,
            PID,
            MID,
            JID,
            request.attempt_number,
            CodexProcessStatus.SUCCEEDED,
            NOW,
            NOW,
            "fake",
            request.timeout_seconds,
            exit_code=0,
            stdout_reference="stdout",
            stderr_reference="stderr",
        ),
    )


def replay_request(worktree: Path, attempt: int = 1) -> CodexRunRequest:
    accepted = ArchitectTask(
        ARCHITECT_TASK_INTERFACE_VERSION,
        "architect-correlation",
        PID,
        MID,
        ArchitectTaskType.IMPLEMENT,
        "Implement exactly",
        ("requirement",),
        ("criterion",),
        (),
        ("pytest",),
        (),
    )
    return CodexRunRequest(
        "1.0",
        "scheduler-correlation",
        PID,
        MID,
        JID,
        attempt,
        worktree,
        accepted.to_dict(),
        AGENTS,
        123,
    )


@pytest.mark.parametrize("already_advanced", [False, True])
def test_successful_replay_repairs_only_missing_transition(
    tmp_path: Path, already_advanced: bool
) -> None:
    path, worktree = tmp_path / "state.db", tmp_path / "worktree"
    control = seed(path, worktree)
    persist_success(control, replay_request(worktree))
    if already_advanced:
        control.execute(
            "UPDATE milestones SET state='VALIDATING_CHANGES' WHERE id=?", (str(MID),)
        )
        control.commit()
    runners: list[RecordingRunner] = []
    result = executor(path, runners).execute(job())
    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert not runners[0].requests and runners[0].validations == [False]
    assert control.execute("SELECT count(*) FROM codex_runs").fetchone()[0] == 1
    assert state(control) == "VALIDATING_CHANGES"


def test_failed_attempt_can_be_followed_by_successful_scheduler_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    control = seed(path, tmp_path / "worktree")
    first = executor(path, [], status=CodexProcessStatus.FAILED).execute(job())
    assert first.disposition is JobExecutionDisposition.FAILED
    retry = job(attempt_number=1)
    result = executor(path, []).execute(retry)
    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    rows = control.execute(
        "SELECT attempt_number,process_status FROM codex_runs ORDER BY attempt_number"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(1, "FAILED"), (2, "SUCCEEDED")]
    assert state(control) == "VALIDATING_CHANGES"
