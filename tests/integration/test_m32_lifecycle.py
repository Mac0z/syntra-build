# ruff: noqa: E501
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.application.lifecycle import LifecycleCoordinator
from syntra_build.domain import (
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
from syntra_build.infrastructure.persistence import apply_migrations, open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

NOW = datetime(2026, 9, 29, tzinfo=UTC)
TS = NOW.isoformat()


def _db(tmp_path: Path) -> sqlite3.Connection:
    db = open_database(tmp_path / f"{uuid4()}.db")
    apply_migrations(db)
    return db


def _project(
    db: sqlite3.Connection, state: ProjectState = ProjectState.BUILDING
) -> ProjectId:
    project_id = ProjectId.generate()
    SQLiteProjectRepository(db, lambda: str(uuid4())).add(
        Project(project_id, "Generated", state, NOW, NOW, canonical_name="generated")
    )
    return project_id


def _milestone(
    db: sqlite3.Connection,
    project_id: ProjectId,
    state: MilestoneState,
    sequence: int = 0,
) -> MilestoneId:
    milestone_id = MilestoneId.generate()
    SQLiteMilestoneRepository(db, lambda: str(uuid4())).add(
        Milestone(
            milestone_id,
            project_id,
            sequence,
            f"M{sequence + 1}",
            "Work",
            state,
            NOW,
            NOW,
        )
    )
    return milestone_id


@pytest.mark.parametrize(
    ("state", "job_type", "worker"),
    [
        ("PREPARING_WORKSPACE", "WORKSPACE_PREPARE", "GIT"),
        ("CODING", "CODEX_RUN", "CODEX"),
        ("VALIDATING_CHANGES", "CHANGE_VALIDATE", "GIT"),
        ("PR_CREATING", "PR_CREATE", "GITHUB"),
        ("ARCHITECT_REVIEW", "ARCHITECT_REVIEW", "ARCHITECT"),
        ("MERGE_READY", "PR_MERGE", "GITHUB"),
    ],
)
def test_state_mapping_and_idempotency(
    tmp_path: Path, state: str, job_type: str, worker: str
) -> None:
    db = _db(tmp_path)
    project = _project(db)
    milestone = _milestone(db, project, MilestoneState(state))
    lifecycle = LifecycleCoordinator(db, clock=lambda: NOW)
    assert lifecycle.enqueue_due() == 1
    assert lifecycle.enqueue_due() == 0
    row = db.execute(
        "SELECT job_type,worker_class FROM jobs WHERE milestone_id=?", (str(milestone),)
    ).fetchone()
    assert tuple(row) == (job_type, worker)


def test_ready_milestone_queues_one_implementation_task(tmp_path: Path) -> None:
    db = _db(tmp_path)
    project = _project(db)
    milestone = _milestone(db, project, MilestoneState.READY)
    lifecycle = LifecycleCoordinator(db, clock=lambda: NOW)
    assert lifecycle.enqueue_due() == 1
    assert lifecycle.enqueue_due() == 0
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(milestone),)
        ).fetchone()[0]
        == "PREPARING_TASK"
    )
    row = db.execute("SELECT job_type,worker_class,payload_json FROM jobs").fetchone()
    assert row[0:2] == ("ARCHITECT_TASK", "ARCHITECT")
    assert json.loads(row[2]) == {"task_type": "IMPLEMENT"}


@pytest.mark.parametrize(
    "state",
    [
        JobState.QUEUED,
        JobState.DISPATCHED,
        JobState.RUNNING,
        JobState.WAITING_EXTERNAL,
        JobState.RETRY_WAIT,
    ],
)
def test_active_review_rework_owns_coding(tmp_path: Path, state: JobState) -> None:
    db = _db(tmp_path)
    project = _project(db)
    milestone = _milestone(db, project, MilestoneState.CODING)
    job_id = JobId.generate()
    SQLiteJobRepository(db, lambda: str(uuid4())).add(
        Job(
            job_id,
            project,
            "CODEX_REVIEW_REWORK",
            state,
            1,
            NOW,
            NOW,
            milestone,
            correlation_id="review-correlation",
            max_attempts=1,
            worker_class=WorkerClass.CODEX,
            payload={
                "task_type": "REVIEW_REWORK",
                "review_id": "review",
                "rework_task_id": "task",
                "pull_request_id": "pr",
            },
        )
    )
    lifecycle = LifecycleCoordinator(db, clock=lambda: NOW)
    assert lifecycle.enqueue_due() == 0
    assert lifecycle.enqueue_due() == 0
    assert (
        db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='CODEX_RUN'").fetchone()[0]
        == 0
    )
    assert (
        db.execute("SELECT state FROM jobs WHERE id=?", (str(job_id),)).fetchone()[0]
        == state.value
    )


@pytest.mark.parametrize(
    "state",
    [
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.ABANDONED,
    ],
)
def test_historical_review_rework_never_falls_back_to_initial_codex(
    tmp_path: Path, state: JobState
) -> None:
    db = _db(tmp_path)
    project = _project(db)
    milestone = _milestone(db, project, MilestoneState.CODING)
    SQLiteJobRepository(db, lambda: str(uuid4())).add(
        Job(
            JobId.generate(),
            project,
            "CODEX_REVIEW_REWORK",
            state,
            1,
            NOW,
            NOW,
            milestone,
            correlation_id="review-correlation",
            max_attempts=1,
            worker_class=WorkerClass.CODEX,
        )
    )
    lifecycle = LifecycleCoordinator(db, clock=lambda: NOW)
    assert lifecycle.enqueue_due() == 0
    assert lifecycle.enqueue_due() == 0
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(milestone),)
        ).fetchone()[0]
        == MilestoneState.CODING.value
    )
    assert (
        db.execute(
            "SELECT state FROM jobs WHERE milestone_id=? AND job_type='CODEX_REVIEW_REWORK'",
            (str(milestone),),
        ).fetchone()[0]
        == state.value
    )
    assert (
        db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='CODEX_RUN'").fetchone()[0]
        == 0
    )


def test_project_scoped_design_job_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    project = _project(db, ProjectState.DESIGNING)
    db.execute(
        """INSERT INTO project_creation_context
           (project_id,owner_id,initial_request,messaging_platform,conversation_id,
            thread_id,source_update_id,source_message_id,created_at)
           VALUES (?,?,?,'telegram','chat',NULL,'update','message',?)""",
        (str(project), "owner", "Build a generated project", TS),
    )
    lifecycle = LifecycleCoordinator(db, clock=lambda: NOW)

    assert lifecycle.enqueue_due() == 1
    row = db.execute(
        """SELECT job_type,milestone_id,worker_class FROM jobs
           WHERE project_id=? AND job_type='ARCHITECT_DESIGN'""",
        (str(project),),
    ).fetchone()
    assert tuple(row) == ("ARCHITECT_DESIGN", None, "ARCHITECT")

    assert lifecycle.enqueue_due() == 0
    assert (
        db.execute(
            """SELECT COUNT(*) FROM jobs WHERE project_id=?
           AND milestone_id IS NULL AND job_type='ARCHITECT_DESIGN'
           AND state IN ('QUEUED','DISPATCHED','RUNNING','WAITING_EXTERNAL','RETRY_WAIT')""",
            (str(project),),
        ).fetchone()[0]
        == 1
    )


def test_materialises_approved_milestones_once_in_sequence(tmp_path: Path) -> None:
    db = _db(tmp_path)
    project = _project(db, ProjectState.READY)
    pid = str(project)
    spec, agents, gate, request, package, repo = (str(uuid4()) for _ in range(6))
    db.execute(
        "INSERT INTO architect_requests(id,project_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,status) VALUES(?,?,'SPECIFICATION_DRAFT','fake','m','high','1.0','{}','c',?,'SUCCEEDED')",
        (request, pid, TS),
    )
    for doc, kind in ((spec, "SPEC"), (agents, "AGENTS")):
        db.execute(
            "INSERT INTO project_documents(id,project_id,document_type,revision,status,content,content_hash,created_at,created_by) VALUES(?,?,?,1,'DRAFT','x',?,?,'ARCHITECT')",
            (doc, pid, kind, "0" * 64, TS),
        )
    db.execute(
        "INSERT INTO human_gates(id,project_id,gate_type,state,title,prompt,expected_response_type,options_json,created_at,created_by,correlation_id) VALUES(?,?,'DESIGN_APPROVAL','RESOLVED','t','p','DESIGN_APPROVAL','[]',?,'SYSTEM','c')",
        (gate, pid, TS),
    )
    db.execute(
        "INSERT INTO design_packages(id,project_id,architect_request_id,spec_document_id,agents_document_id,repository_visibility,design_summary,planned_milestones_json,assumptions_json,non_blocking_issues_json,status,approval_gate_id,created_at,approved_at,approved_by) VALUES(?,?,?,?,?,'public','summary',?,'[]','[]','APPROVED',?,?,?,'human')",
        (
            package,
            pid,
            request,
            spec,
            agents,
            json.dumps(
                [{"code": "M1", "title": "One"}, {"code": "M2", "title": "Two"}]
            ),
            gate,
            TS,
            TS,
        ),
    )
    db.execute(
        "INSERT INTO github_repositories(id,project_id,provider,owner,repository_name,full_name,external_repository_id,visibility,default_branch,status,created_at,updated_at,verified_at) VALUES(?,?,'github','o','r','o/r',1,'public','main','VERIFIED',?,?,?)",
        (repo, pid, TS, TS, TS),
    )
    lifecycle = LifecycleCoordinator(db, clock=lambda: NOW)
    assert lifecycle.enqueue_due() == 1
    assert (
        db.execute("SELECT state FROM projects WHERE id=?", (pid,)).fetchone()[0]
        == "BUILDING"
    )
    assert [
        tuple(r)
        for r in db.execute(
            "SELECT code,state FROM milestones ORDER BY sequence_number"
        )
    ] == [("M1", "READY"), ("M2", "PENDING")]
    assert db.execute("SELECT COUNT(*) FROM milestone_dependencies").fetchone()[0] == 1
    lifecycle.enqueue_due()
    assert db.execute("SELECT COUNT(*) FROM milestones").fetchone()[0] == 2


def test_sequence_completion_and_blockers(tmp_path: Path) -> None:
    db = _db(tmp_path)
    project = _project(db)
    first = _milestone(db, project, MilestoneState.COMPLETE)
    second = _milestone(db, project, MilestoneState.PENDING, 1)
    SQLiteMilestoneRepository(db, lambda: str(uuid4())).add_dependency(second, first)
    assert LifecycleCoordinator(db, clock=lambda: NOW).enqueue_due() == 1
    assert (
        db.execute(
            "SELECT state FROM milestones WHERE id=?", (str(second),)
        ).fetchone()[0]
        == "READY"
    )


@pytest.mark.parametrize("blocker", ["gate", "security", None])
def test_project_completion_requires_no_blocker(
    tmp_path: Path, blocker: str | None
) -> None:
    db = _db(tmp_path)
    project = _project(db)
    _milestone(db, project, MilestoneState.COMPLETE)
    pid = str(project)
    if blocker == "gate":
        db.execute(
            "INSERT INTO human_gates(id,project_id,gate_type,state,title,prompt,expected_response_type,options_json,created_at,created_by,correlation_id) VALUES(?,?,'HUMAN_TEST','PENDING','t','p','HUMAN_TEST','[]',?,'SYSTEM','c')",
            (str(uuid4()), pid, TS),
        )
    elif blocker == "security":
        db.execute(
            "INSERT INTO security_events(id,event_type,severity,project_id,source_component,correlation_id,safe_details_json,blocking,created_at) VALUES(?,'SECRET_DETECTED','HIGH',?,'test','c','{}',1,?)",
            (str(uuid4()), pid, TS),
        )
    LifecycleCoordinator(db, clock=lambda: NOW).enqueue_due()
    expected = "BUILDING" if blocker else "COMPLETE"
    assert (
        db.execute("SELECT state FROM projects WHERE id=?", (pid,)).fetchone()[0]
        == expected
    )


@pytest.mark.parametrize(
    "state",
    [
        ProjectState.PAUSED,
        ProjectState.CANCELLED,
        ProjectState.FAILED,
        ProjectState.COMPLETE,
    ],
)
def test_ineligible_projects_are_ignored(tmp_path: Path, state: ProjectState) -> None:
    db = _db(tmp_path)
    _project(db, state)
    assert LifecycleCoordinator(db, clock=lambda: NOW).enqueue_due() == 0
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
