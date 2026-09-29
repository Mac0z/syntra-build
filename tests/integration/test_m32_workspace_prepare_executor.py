# ruff: noqa: E501
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from syntra_build.application.m32_executors import WorkspacePrepareExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.application.workspaces import WorkspaceService, milestone_branch_name
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
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import (
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 9, 29, 16, tzinfo=UTC)
PID = ProjectId(UUID(int=501))
MID = MilestoneId(UUID(int=502))
JID = JobId(UUID(int=503))
BASE = "a" * 40
BRANCH = "syntra/m00-workspace-milestone"
SPEC_ID = "00000000-0000-0000-0000-000000000504"
AGENTS_ID = "00000000-0000-0000-0000-000000000505"


class FakeTrustedGit:
    def __init__(self) -> None:
        self.branches: dict[str, str] = {}
        self.registered_paths: set[Path] = set()
        self.worktree_branches: dict[Path, str] = {}
        self.ensure_calls: list[tuple[Path, str]] = []
        self.fetch_calls: list[tuple[Path, str, str]] = []
        self.add_calls: list[tuple[Path, Path, str]] = []

    def ensure_bare(self, path: Path, remote_url: str) -> None:
        self.ensure_calls.append((path, remote_url))
        path.mkdir(parents=True, exist_ok=True)

    def fetch(self, path: Path, remote_url: str, branch: str) -> str:
        self.fetch_calls.append((path, remote_url, branch))
        return BASE

    def branch_sha(self, repository: Path, branch: str) -> str | None:
        return self.branches.get(branch)

    def has_commit(self, repository: Path, sha: str) -> bool:
        return sha == BASE

    def fetch_branch(self, repository: Path, remote_url: str, branch: str) -> str:
        return BASE

    def create_branch(self, repository: Path, branch: str, base_sha: str) -> None:
        self.branches[branch] = base_sha

    def add_worktree(self, repository: Path, path: Path, branch: str) -> None:
        self.add_calls.append((repository, path, branch))
        path.mkdir(parents=True)
        self.registered_paths.add(path)
        self.worktree_branches[path] = branch

    def registered(self, repository: Path, path: Path) -> bool:
        return path in self.registered_paths

    def prune_worktrees(self, repository: Path) -> None:
        self.registered_paths = {
            path for path in self.registered_paths if path.exists()
        }

    def origin(self, worktree: Path) -> str:
        return "https://github.com/Mac0z/generated-app.git"

    def branch(self, worktree: Path) -> str:
        return self.worktree_branches[worktree]

    def head(self, worktree: Path) -> str:
        return self.branches[self.worktree_branches[worktree]]

    def changes(
        self, worktree: Path
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        return (), (), ()

    def remote_branch_sha(
        self, repository: Path, remote_url: str, branch: str
    ) -> str | None:
        return None


def _job(**changes: object) -> Job:
    value = Job(
        JID,
        PID,
        "WORKSPACE_PREPARE",
        JobState.RUNNING,
        1,
        NOW,
        NOW,
        MID,
        correlation_id="workspace-correlation",
        max_attempts=3,
        worker_class=WorkerClass.GIT,
        payload={},
    )
    return replace(value, **changes)  # type: ignore[arg-type]


def _seed(
    path: Path,
    *,
    task: bool = True,
    project_state: ProjectState = ProjectState.BUILDING,
    milestone_state: MilestoneState = MilestoneState.PREPARING_WORKSPACE,
    repository_status: str = "VERIFIED",
) -> sqlite3.Connection:
    db = open_database(path)
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "transition").add(
        Project(PID, "Workspace project", project_state, NOW, NOW)
    )
    SQLiteMilestoneRepository(db, lambda: "transition").add(
        Milestone(MID, PID, 0, "M1", "Workspace milestone", milestone_state, NOW, NOW)
    )
    now = NOW.isoformat()
    db.execute(
        """INSERT INTO github_repositories
        (id,project_id,provider,owner,repository_name,full_name,external_repository_id,
         visibility,default_branch,status,created_at,updated_at,verified_at)
        VALUES ('repository',?,'github','Mac0z','generated-app','Mac0z/generated-app',321,
                'public','main',?,?,?,?)""",
        (
            str(PID),
            repository_status,
            now,
            now,
            now if repository_status == "VERIFIED" else None,
        ),
    )
    if repository_status == "VERIFIED":
        for document_id, document_type in ((SPEC_ID, "SPEC"), (AGENTS_ID, "AGENTS")):
            db.execute(
                """INSERT INTO project_documents
                (id,project_id,document_type,revision,status,content,content_hash,
                 created_at,created_by,approved_at,approved_by)
                VALUES (?,?,?,1,'APPROVED','# approved',?,?,'architect',?,'human')""",
                (document_id, str(PID), document_type, "f" * 64, now, now),
            )
        db.execute(
            """INSERT INTO repository_baselines
            (id,project_id,github_repository_id,commit_sha,spec_document_id,
             spec_revision,spec_content_hash,agents_document_id,agents_revision,
             agents_content_hash,created_at,verified_at)
            VALUES ('baseline',?,'repository',?,?,1,?,?,1,?,?,?)""",
            (str(PID), BASE, SPEC_ID, "f" * 64, AGENTS_ID, "f" * 64, now, now),
        )
    if task:
        response = ArchitectTask(
            ARCHITECT_TASK_INTERFACE_VERSION,
            "task-correlation",
            PID,
            MID,
            ArchitectTaskType.IMPLEMENT,
            "Implement",
            ("Requirement",),
            ("Done",),
            (),
            ("Test",),
            (),
        )
        db.execute(
            """INSERT INTO architect_requests
            (id,project_id,milestone_id,request_type,provider,model,reasoning_level,
             request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status)
            VALUES ('task-request',?,?,'TASK','fake','fake','high','1.0','{}','task-correlation',?,?,'SUCCEEDED')""",
            (str(PID), str(MID), now, now),
        )
        db.execute(
            """INSERT INTO architect_responses
            (id,architect_request_id,response_type,response_schema_version,normalised_payload_json,
             status,created_at,validation_status,provider,model)
            VALUES ('task-response','task-request','TASK','1.0',?,'ACCEPTED',?,'VALID','fake','fake')""",
            (json.dumps(response.to_dict()), now),
        )
    db.commit()
    return db


def _executor(
    path: Path,
    data_root: Path,
    git: FakeTrustedGit,
    workers: list[sqlite3.Connection] | None = None,
) -> WorkspacePrepareExecutor:
    def connections(database_path: Path) -> sqlite3.Connection:
        connection = open_database(database_path)
        if workers is not None:
            workers.append(connection)
        return connection

    def services(connection: sqlite3.Connection) -> WorkspaceService:
        return WorkspaceService(connection, cast(TrustedGit, git), data_root)

    return WorkspacePrepareExecutor(
        path, services, clock=lambda: NOW, connection_factory=connections
    )


def test_prepares_verified_workspace_on_worker_connection_and_advances(
    tmp_path: Path,
) -> None:
    path, data = tmp_path / "state.db", tmp_path / "data"
    control = _seed(path)
    git = FakeTrustedGit()
    workers: list[sqlite3.Connection] = []

    result = _executor(path, data, git, workers).execute(_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert len(workers) == 1 and workers[0] is not control
    managed = control.execute("SELECT * FROM git_repositories").fetchone()
    workspace = control.execute("SELECT * FROM git_workspaces").fetchone()
    assert managed["remote_url"] == "https://github.com/Mac0z/generated-app.git"
    assert tuple(git.fetch_calls[0][1:]) == (managed["remote_url"], "main")
    assert (
        workspace["branch_name"]
        == milestone_branch_name(0, "Workspace milestone")
        == BRANCH
    )
    assert Path(workspace["worktree_path"]).is_relative_to(data / "workspaces")
    assert (
        workspace["state"],
        workspace["base_sha"],
        workspace["current_head_sha"],
    ) == ("READY", BASE, BASE)
    assert len(git.add_calls) == 1
    assert (
        control.execute(
            "SELECT state FROM milestones WHERE id=?", (str(MID),)
        ).fetchone()[0]
        == "CODING"
    )
    transition = control.execute(
        "SELECT correlation_id,actor_type FROM state_transitions WHERE entity_type='MILESTONE'"
    ).fetchone()
    assert tuple(transition) == ("workspace-correlation", "SYSTEM")


@pytest.mark.parametrize(
    "changes",
    [
        {"job_type": "CODEX_RUN"},
        {"worker_class": WorkerClass.CODEX},
        {"milestone_id": None},
        {"payload": {"path": "/tmp/untrusted"}},
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

    executor = WorkspacePrepareExecutor(
        tmp_path / "absent.db",
        lambda _db: cast(WorkspaceService, object()),
        connection_factory=connections,
    )
    with pytest.raises(ValueError):
        executor.execute(_job(**changes))
    assert not opened


@pytest.mark.parametrize(
    "project_state,milestone_state",
    [
        (ProjectState.READY, MilestoneState.PREPARING_WORKSPACE),
        (ProjectState.BUILDING, MilestoneState.PREPARING_TASK),
    ],
)
def test_wrong_authoritative_state_fails_before_git(
    tmp_path: Path, project_state: ProjectState, milestone_state: MilestoneState
) -> None:
    path = tmp_path / "state.db"
    control = _seed(path, project_state=project_state, milestone_state=milestone_state)
    git = FakeTrustedGit()
    with pytest.raises(ValueError):
        _executor(path, tmp_path / "data", git).execute(_job())
    assert not git.ensure_calls
    assert control.execute("SELECT count(*) FROM git_workspaces").fetchone()[0] == 0


def test_missing_task_fails_before_git(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _seed(path, task=False)
    git = FakeTrustedGit()
    with pytest.raises(Exception, match="accepted Architect task"):
        _executor(path, tmp_path / "data", git).execute(_job())
    assert not git.ensure_calls


def test_unverified_repository_fails_without_workspace_success(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    control = _seed(path, repository_status="INTENDED")
    git = FakeTrustedGit()
    with pytest.raises(ValueError, match="verified M18 repository baseline"):
        _executor(path, tmp_path / "data", git).execute(_job())
    assert not git.ensure_calls
    assert (
        control.execute(
            "SELECT state FROM milestones WHERE id=?", (str(MID),)
        ).fetchone()[0]
        == "PREPARING_WORKSPACE"
    )


def test_unowned_path_is_not_adopted_or_deleted(tmp_path: Path) -> None:
    path, data = tmp_path / "state.db", tmp_path / "data"
    control = _seed(path)
    unexpected = data / "workspaces" / str(PID) / str(MID)
    unexpected.mkdir(parents=True)
    marker = unexpected / "owner-data"
    marker.write_text("preserve")
    result = _executor(path, data, FakeTrustedGit()).execute(_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    assert marker.read_text() == "preserve"
    assert control.execute("SELECT count(*) FROM git_workspaces").fetchone()[0] == 0
    assert (
        control.execute(
            "SELECT state FROM milestones WHERE id=?", (str(MID),)
        ).fetchone()[0]
        == "PREPARING_WORKSPACE"
    )


def test_existing_workspace_replay_reconciles_without_duplicates(
    tmp_path: Path,
) -> None:
    path, data = tmp_path / "state.db", tmp_path / "data"
    control = _seed(path)
    git = FakeTrustedGit()
    executor = _executor(path, data, git)
    assert executor.execute(_job()).disposition is JobExecutionDisposition.SUCCEEDED
    assert executor.execute(_job()).disposition is JobExecutionDisposition.SUCCEEDED
    assert control.execute("SELECT count(*) FROM git_repositories").fetchone()[0] == 1
    assert control.execute("SELECT count(*) FROM git_workspaces").fetchone()[0] == 1
    assert len(git.add_calls) == 1
    assert git.branches == {BRANCH: BASE}
    assert (
        control.execute(
            "SELECT state FROM milestones WHERE id=?", (str(MID),)
        ).fetchone()[0]
        == "CODING"
    )


def test_persisted_identity_and_head_mismatches_fail_closed(tmp_path: Path) -> None:
    path, data = tmp_path / "state.db", tmp_path / "data"
    control = _seed(path)
    git = FakeTrustedGit()
    executor = _executor(path, data, git)
    assert executor.execute(_job()).disposition is JobExecutionDisposition.SUCCEEDED
    control.execute(
        "UPDATE milestones SET state='PREPARING_WORKSPACE' WHERE id=?", (str(MID),)
    )
    trigger = control.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='git_workspaces' AND sql LIKE '%immutable%'"
    ).fetchone()
    assert trigger is not None
    control.execute(f'DROP TRIGGER "{trigger[0]}"')
    control.execute(
        "UPDATE git_workspaces SET branch_name='syntra/corrupt' WHERE milestone_id=?",
        (str(MID),),
    )
    control.commit()
    result = executor.execute(_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    assert (
        control.execute(
            "SELECT state FROM milestones WHERE id=?", (str(MID),)
        ).fetchone()[0]
        == "PREPARING_WORKSPACE"
    )
    assert (
        control.execute("SELECT branch_name FROM git_workspaces").fetchone()[0]
        == "syntra/corrupt"
    )

    control.execute(
        "UPDATE git_workspaces SET branch_name=? WHERE milestone_id=?",
        (BRANCH, str(MID)),
    )
    control.commit()
    git.branches[BRANCH] = "b" * 40
    result = executor.execute(_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    row = control.execute(
        "SELECT state,current_head_sha FROM git_workspaces"
    ).fetchone()
    assert tuple(row) == ("ERROR", "b" * 40)
    assert (
        control.execute(
            "SELECT state FROM milestones WHERE id=?", (str(MID),)
        ).fetchone()[0]
        == "PREPARING_WORKSPACE"
    )
