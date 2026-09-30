from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from syntra_build.application.change_validation import ChangeValidationService
from syntra_build.application.m32_executors import TrustedCommitExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.application.workspaces import WorkspaceService
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
from syntra_build.domain.change_validation import ValidationDecision
from syntra_build.domain.codex import (
    CodexProcessStatus,
    CodexRunRequest,
    CodexRunResult,
)
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.workspaces import ManagedRepository, Workspace, WorkspaceState
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)
from syntra_build.infrastructure.persistence.codex import SQLiteCodexRunRepository
from syntra_build.infrastructure.persistence.workspaces import SQLiteWorkspaceRepository

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
PID = ProjectId(UUID(int=701))
MID = MilestoneId(UUID(int=702))
VALIDATION_JOB_ID = JobId(UUID(int=703))
COMMIT_JOB_ID = JobId(UUID(int=705))
CODEX_JOB_ID = JobId(UUID(int=704))
CORRELATION = "m32-7-correlation"


def git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def validation_job(**changes: object) -> Job:
    value = Job(
        VALIDATION_JOB_ID,
        PID,
        "CHANGE_VALIDATE",
        JobState.RUNNING,
        0,
        NOW,
        NOW,
        MID,
        correlation_id=CORRELATION,
        max_attempts=3,
        worker_class=WorkerClass.GIT,
        payload={},
    )
    return replace(value, **changes)  # type: ignore[arg-type]


def seed(
    tmp_path: Path, *, successful_codex: bool = True
) -> tuple[Path, Path, TrustedGit]:
    data = tmp_path / "data"
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    (source / "existing.txt").write_text("initial\n")
    git(source, "add", ".")
    identity = dict(
        os.environ,
        GIT_AUTHOR_NAME="Syntra test",
        GIT_AUTHOR_EMAIL="syntra@example.test",
        GIT_COMMITTER_NAME="Syntra test",
        GIT_COMMITTER_EMAIL="syntra@example.test",
    )
    git(source, "commit", "-m", "initial", env=identity)
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main")

    database = tmp_path / "state.db"
    db = open_database(database)
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "project-transition").add(
        Project(PID, "Validation project", ProjectState.BUILDING, NOW, NOW)
    )
    SQLiteMilestoneRepository(db, lambda: "milestone-transition").add(
        Milestone(
            MID,
            PID,
            0,
            "M1",
            "Validate",
            MilestoneState.COMMITTING,
            NOW,
            NOW,
        )
    )
    codex_job = Job(
        CODEX_JOB_ID,
        PID,
        "CODEX_RUN",
        JobState.SUCCEEDED,
        1,
        NOW,
        NOW,
        MID,
        correlation_id="codex-correlation",
        max_attempts=3,
        worker_class=WorkerClass.CODEX,
        payload={},
    )
    SQLiteJobRepository(db, lambda: "job-transition").add(codex_job)

    trusted = TrustedGit(data / "auth")
    managed_path = data / "repositories" / str(PID) / "repo.git"
    trusted.ensure_bare(managed_path, str(remote))
    head = trusted.fetch(managed_path, str(remote), "main")
    branch = "syntra/m00-validate"
    trusted.create_branch(managed_path, branch, head)
    worktree = data / "workspaces" / str(PID) / str(MID)
    trusted.add_worktree(managed_path, worktree, branch)
    db.execute(
        """INSERT INTO github_repositories
        (id,project_id,provider,owner,repository_name,full_name,
         external_repository_id,visibility,default_branch,status,
         created_at,updated_at,verified_at)
        VALUES ('gh',?,'github','owner','repo','owner/repo',1,'public',
                'main','VERIFIED',?,?,?)""",
        (str(PID), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    workspaces = SQLiteWorkspaceRepository(db)
    workspaces.create_managed(
        ManagedRepository(
            "repo", PID, "gh", managed_path, str(remote), "main", NOW, head
        ),
        NOW,
    )
    workspaces.create_workspace(
        Workspace(
            "workspace",
            PID,
            MID,
            "repo",
            branch,
            worktree,
            "main",
            head,
            head,
            WorkspaceState.READY,
            NOW,
        )
    )
    if successful_codex:
        request = CodexRunRequest(
            "1.0",
            "codex-correlation",
            PID,
            MID,
            CODEX_JOB_ID,
            1,
            worktree,
            {"task_type": "IMPLEMENT"},
            "# Instructions",
            60,
        )
        runs = SQLiteCodexRunRepository(db)
        runs.start("codex-run", request, "workspace", "f" * 64, NOW, "stdout", "stderr")
        runs.complete(
            "codex-run",
            CodexRunResult(
                "1.0",
                "codex-correlation",
                PID,
                MID,
                CODEX_JOB_ID,
                1,
                CodexProcessStatus.SUCCEEDED,
                NOW,
                NOW,
                "fake-codex",
                60,
                exit_code=0,
            ),
        )
    db.commit()
    db.close()
    return database, worktree, trusted


def milestone_state(database: Path) -> str:
    with open_database(database) as db:
        return cast(
            str,
            db.execute(
                "SELECT state FROM milestones WHERE id=?", (str(MID),)
            ).fetchone()[0],
        )


def commit_job(**changes: object) -> Job:
    value = Job(
        COMMIT_JOB_ID,
        PID,
        "GIT_COMMIT",
        JobState.RUNNING,
        0,
        NOW,
        NOW,
        MID,
        correlation_id=CORRELATION,
        max_attempts=3,
        worker_class=WorkerClass.GIT,
        payload={},
    )
    return replace(value, **changes)  # type: ignore[arg-type]


def prepare(tmp_path: Path) -> tuple[Path, Path, TrustedGit, str, str]:
    database, worktree, trusted = seed(tmp_path)
    (worktree / "safe.py").write_text("answer = 42\n")
    with open_database(database) as db:
        change_set = ChangeValidationService(db, trusted, tmp_path / "data").validate(
            PID, MID, CORRELATION, now=NOW
        )
        db.commit()
    assert change_set.decision is ValidationDecision.ACCEPT
    return (
        database,
        worktree,
        trusted,
        change_set.trusted_head_sha,
        change_set.diff_hash,
    )


def executor(database: Path, trusted: TrustedGit, data: Path) -> TrustedCommitExecutor:
    return TrustedCommitExecutor(database, trusted, data, clock=lambda: NOW)


def test_success_commits_accepted_files_and_advances(tmp_path: Path) -> None:
    database, worktree, trusted, parent, diff_hash = prepare(tmp_path)

    result = executor(database, trusted, tmp_path / "data").execute(commit_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        commit = db.execute("SELECT * FROM commits").fetchone()
        evidence = db.execute("SELECT * FROM change_sets").fetchone()
        workspace = db.execute("SELECT * FROM git_workspaces").fetchone()
        assert commit["parent_sha"] == parent
        assert commit["message"] == "M1: Validate"
        assert commit["change_set_id"] == evidence["id"]
        assert commit["validated_diff_hash"] == diff_hash
        assert commit["pushed_at"] is None
        assert workspace["current_head_sha"] == commit["commit_sha"]
        assert milestone_state(database) == MilestoneState.PUSHING.value
    assert git(worktree, "status", "--porcelain") == ""
    assert git(worktree, "show", "--pretty=", "--name-only", "HEAD") == "safe.py"


@pytest.mark.parametrize(
    "changes",
    [
        {"job_type": "GIT_PUSH"},
        {"worker_class": WorkerClass.CODEX},
        {"milestone_id": None},
        {"payload": {"paths": ["safe.py"]}},
    ],
)
def test_invalid_envelope_opens_no_database(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    opened = False

    def connection_factory(_path: Path) -> sqlite3.Connection:
        nonlocal opened
        opened = True
        raise AssertionError

    subject = TrustedCommitExecutor(
        tmp_path / "absent.db",
        object(),  # type: ignore[arg-type]
        tmp_path,
        connection_factory=connection_factory,
    )
    with pytest.raises(ValueError):
        subject.execute(commit_job(**changes))
    assert not opened


def test_changed_diff_fails_without_commit_or_transition(tmp_path: Path) -> None:
    database, worktree, trusted, parent, _ = prepare(tmp_path)
    (worktree / "safe.py").write_text("changed after accept\n")

    result = executor(database, trusted, tmp_path / "data").execute(commit_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.PERMANENT
    with open_database(database) as db:
        assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 0
    assert git(worktree, "rev-parse", "HEAD") == parent
    assert milestone_state(database) == MilestoneState.COMMITTING.value


def test_wrong_project_or_milestone_state_prevents_git_mutation(tmp_path: Path) -> None:
    database, worktree, trusted, parent, _ = prepare(tmp_path)
    with open_database(database) as db:
        db.execute("UPDATE projects SET state='PAUSED' WHERE id=?", (str(PID),))
        db.commit()
    with pytest.raises(ValueError, match="BUILDING"):
        executor(database, trusted, tmp_path / "data").execute(commit_job())
    assert git(worktree, "rev-parse", "HEAD") == parent
    with open_database(database) as db:
        db.execute("UPDATE projects SET state='BUILDING' WHERE id=?", (str(PID),))
        db.execute("UPDATE milestones SET state='CODING' WHERE id=?", (str(MID),))
        db.commit()
    with pytest.raises(ValueError, match="COMMITTING"):
        executor(database, trusted, tmp_path / "data").execute(commit_job())
    assert git(worktree, "rev-parse", "HEAD") == parent


def test_missing_or_empty_accept_evidence_fails_closed(tmp_path: Path) -> None:
    database, worktree, trusted = seed(tmp_path)
    parent = git(worktree, "rev-parse", "HEAD")
    (worktree / "safe.py").write_text("answer = 42\n")
    missing = executor(database, trusted, tmp_path / "data").execute(commit_job())
    assert missing.disposition is JobExecutionDisposition.FAILED
    assert git(worktree, "rev-parse", "HEAD") == parent

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    database, worktree, trusted, parent, _ = prepare(empty_root)
    with open_database(database) as db:
        db.execute(
            """INSERT INTO change_sets SELECT
            'empty-evidence',interface_version,project_id,milestone_id,worktree_id,
            branch_name,base_sha,head_sha_before_commit,diff_hash,0,'[]',decision,
            correlation_id,scanner_version,policy_version,'2026-09-30T13:00:00+00:00'
            FROM change_sets LIMIT 1"""
        )
        db.commit()
    empty = executor(database, trusted, empty_root / "data").execute(commit_job())
    assert empty.disposition is JobExecutionDisposition.FAILED
    assert git(worktree, "rev-parse", "HEAD") == parent


def test_persisted_commit_replays_without_second_commit(tmp_path: Path) -> None:
    database, worktree, trusted, _, _ = prepare(tmp_path)
    with open_database(database) as db:
        service = WorkspaceService(db, trusted, tmp_path / "data")
        evidence = db.execute("SELECT * FROM change_sets").fetchone()
        service.commit(
            PID,
            MID,
            evidence["head_sha_before_commit"],
            ("safe.py",),
            "M1: Validate",
            NOW,
            expected_diff_hash=evidence["diff_hash"],
        )
        db.commit()
    original_head = git(worktree, "rev-parse", "HEAD")

    result = executor(database, trusted, tmp_path / "data").execute(commit_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert git(worktree, "rev-parse", "HEAD") == original_head
    with open_database(database) as db:
        assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 1
    assert milestone_state(database) == MilestoneState.PUSHING.value
    again = executor(database, trusted, tmp_path / "data").execute(commit_job())
    assert again.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 1


def test_unexplained_live_commit_is_not_adopted(tmp_path: Path) -> None:
    database, worktree, trusted, _, _ = prepare(tmp_path)
    identity = dict(
        os.environ,
        GIT_AUTHOR_NAME="tamper",
        GIT_AUTHOR_EMAIL="tamper@example.test",
        GIT_COMMITTER_NAME="tamper",
        GIT_COMMITTER_EMAIL="tamper@example.test",
    )
    git(worktree, "add", "safe.py")
    git(worktree, "commit", "-m", "untrusted", env=identity)

    result = executor(database, trusted, tmp_path / "data").execute(commit_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    with open_database(database) as db:
        assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 0
    assert milestone_state(database) == MilestoneState.COMMITTING.value


def test_replay_rejects_live_git_tamper(tmp_path: Path) -> None:
    database, worktree, trusted, _, _ = prepare(tmp_path)
    first = executor(database, trusted, tmp_path / "data").execute(commit_job())
    assert first.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        db.execute("UPDATE milestones SET state='COMMITTING' WHERE id=?", (str(MID),))
        db.commit()
    git(worktree, "checkout", "--detach", "HEAD~1")

    replay = executor(database, trusted, tmp_path / "data").execute(commit_job())

    assert replay.disposition is JobExecutionDisposition.FAILED
    with open_database(database) as db:
        assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 1
    assert milestone_state(database) == MilestoneState.COMMITTING.value
