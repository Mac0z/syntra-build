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
from syntra_build.application.m32_executors import ChangeValidationExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
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
from syntra_build.domain.change_validation import FindingCode, ValidationDecision
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
            MilestoneState.VALIDATING_CHANGES,
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


def executor(
    database: Path, trusted: TrustedGit, data: Path
) -> ChangeValidationExecutor:
    return ChangeValidationExecutor(database, trusted, data, clock=lambda: NOW)


def milestone_state(database: Path) -> str:
    with open_database(database) as db:
        return cast(
            str,
            db.execute(
                "SELECT state FROM milestones WHERE id=?", (str(MID),)
            ).fetchone()[0],
        )


def test_accept_persists_exact_evidence_and_advances(tmp_path: Path) -> None:
    database, worktree, trusted = seed(tmp_path)
    (worktree / "safe.py").write_text("answer = 42\n")

    result = executor(database, trusted, tmp_path / "data").execute(validation_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        row = db.execute("SELECT * FROM change_sets").fetchone()
        assert row["decision"] == ValidationDecision.ACCEPT.value
        assert row["is_empty"] == 0
        assert row["diff_hash"].startswith("sha256:")
        assert row["correlation_id"] == CORRELATION
        assert "safe.py" in row["files_json"]
        evidence = db.execute(
            "SELECT id FROM change_sets WHERE worktree_id=? "
            "AND head_sha_before_commit=? AND diff_hash=?",
            ("workspace", row["head_sha_before_commit"], row["diff_hash"]),
        ).fetchone()
        assert evidence is not None
    assert milestone_state(database) == MilestoneState.COMMITTING.value
    assert git(worktree, "status", "--porcelain") == "?? safe.py"


@pytest.mark.parametrize(
    "changes",
    (
        {"job_type": "GIT_COMMIT"},
        {"worker_class": WorkerClass.CODEX},
        {"milestone_id": None},
        {"payload": {"scope": "arbitrary"}},
    ),
)
def test_invalid_job_is_rejected_before_opening_database(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    opened = False

    def connection_factory(_path: Path) -> sqlite3.Connection:
        nonlocal opened
        opened = True
        raise AssertionError("must not open")

    instance = ChangeValidationExecutor(
        tmp_path / "missing.db",
        TrustedGit(tmp_path / "auth"),
        tmp_path / "data",
        connection_factory=connection_factory,
    )
    with pytest.raises(ValueError):
        instance.execute(validation_job(**changes))
    assert not opened


def test_authoritative_preconditions_fail_without_validation(tmp_path: Path) -> None:
    database, _worktree, trusted = seed(tmp_path, successful_codex=False)
    with pytest.raises(ValueError, match="Codex"):
        executor(database, trusted, tmp_path / "data").execute(validation_job())
    with open_database(database) as db:
        assert db.execute("SELECT count(*) FROM change_sets").fetchone()[0] == 0
    assert milestone_state(database) == MilestoneState.VALIDATING_CHANGES.value

    with open_database(database) as db:
        db.execute("UPDATE projects SET state='PAUSED' WHERE id=?", (str(PID),))
        db.commit()
    with pytest.raises(ValueError, match="BUILDING"):
        executor(database, trusted, tmp_path / "data").execute(validation_job())


def test_wrong_milestone_state_is_rejected(tmp_path: Path) -> None:
    database, _worktree, trusted = seed(tmp_path)
    with open_database(database) as db:
        db.execute("UPDATE milestones SET state='CODING' WHERE id=?", (str(MID),))
        db.commit()
    with pytest.raises(ValueError, match="VALIDATING_CHANGES"):
        executor(database, trusted, tmp_path / "data").execute(validation_job())


def test_empty_change_set_is_durable_permanent_rework(tmp_path: Path) -> None:
    database, _worktree, trusted = seed(tmp_path)
    result = executor(database, trusted, tmp_path / "data").execute(validation_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.PERMANENT
    with open_database(database) as db:
        assert (
            db.execute("SELECT decision FROM change_sets").fetchone()[0]
            == "REWORK_REQUIRED"
        )
    assert milestone_state(database) == MilestoneState.VALIDATING_CHANGES.value


@pytest.mark.parametrize(
    ("relative_path", "content", "decision", "finding"),
    (
        (
            "config.env",
            "api_key='SYNTHETIC_FAKE_VALUE_12345'\n",
            "REWORK_REQUIRED",
            FindingCode.SECRET_DETECTED,
        ),
        (
            ".github/workflows/ci.yml",
            "name: prohibited\n",
            "REWORK_REQUIRED",
            FindingCode.PROTECTED_PATH_CHANGE,
        ),
    ),
)
def test_policy_findings_do_not_advance(
    tmp_path: Path,
    relative_path: str,
    content: str,
    decision: str,
    finding: FindingCode,
) -> None:
    database, worktree, trusted = seed(tmp_path)
    changed = worktree / relative_path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text(content)
    result = executor(database, trusted, tmp_path / "data").execute(validation_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    with open_database(database) as db:
        assert db.execute("SELECT decision FROM change_sets").fetchone()[0] == decision
        assert (
            db.execute("SELECT finding_code FROM validation_findings").fetchone()[0]
            == finding.value
        )
        assert db.execute("SELECT blocking FROM security_events").fetchone()[0] == 1
    assert milestone_state(database) == MilestoneState.VALIDATING_CHANGES.value


def test_repository_identity_and_history_fail_closed(tmp_path: Path) -> None:
    database, worktree, trusted = seed(tmp_path)
    (worktree / "safe.py").write_text("safe = True\n")
    git(worktree, "remote", "set-url", "origin", "https://example.test/wrong.git")
    result = executor(database, trusted, tmp_path / "data").execute(validation_job())
    assert result.failure_classification is FailureClassification.POLICY
    with open_database(database) as db:
        assert db.execute("SELECT decision FROM change_sets").fetchone()[0] == "BLOCKED"
        assert db.execute("SELECT count(*) FROM security_events").fetchone()[0] >= 1
    assert milestone_state(database) == MilestoneState.VALIDATING_CHANGES.value


def test_unexpected_history_and_workspace_escape_are_blocked(tmp_path: Path) -> None:
    database, worktree, trusted = seed(tmp_path)
    git(worktree, "checkout", "-b", "unexpected")
    (worktree / "escape").symlink_to(tmp_path / "outside")
    result = executor(database, trusted, tmp_path / "data").execute(validation_job())
    assert result.failure_classification is FailureClassification.POLICY
    with open_database(database) as db:
        codes = {
            row[0] for row in db.execute("SELECT finding_code FROM validation_findings")
        }
        assert FindingCode.UNEXPECTED_GIT_HISTORY_CHANGE.value in codes
        assert FindingCode.WORKSPACE_ESCAPE_ATTEMPT.value in codes
    assert milestone_state(database) == MilestoneState.VALIDATING_CHANGES.value


def test_exact_replay_repairs_transition_without_duplicate(tmp_path: Path) -> None:
    database, worktree, trusted = seed(tmp_path)
    (worktree / "safe.py").write_text("safe = True\n")
    with open_database(database) as db:
        accepted = ChangeValidationService(db, trusted, tmp_path / "data").validate(
            PID, MID, CORRELATION, now=NOW
        )
        assert accepted.decision is ValidationDecision.ACCEPT
        before = db.execute("SELECT count(*) FROM change_sets").fetchone()[0]

    result = executor(database, trusted, tmp_path / "data").execute(validation_job())
    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        assert db.execute("SELECT count(*) FROM change_sets").fetchone()[0] == before
    assert milestone_state(database) == MilestoneState.COMMITTING.value

    replay = executor(database, trusted, tmp_path / "data").execute(validation_job())
    assert replay.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        assert db.execute("SELECT count(*) FROM change_sets").fetchone()[0] == before


def test_changed_diff_after_accept_creates_fresh_authoritative_evidence(
    tmp_path: Path,
) -> None:
    database, worktree, trusted = seed(tmp_path)
    changed = worktree / "safe.py"
    changed.write_text("version = 1\n")
    with open_database(database) as db:
        first = ChangeValidationService(db, trusted, tmp_path / "data").validate(
            PID, MID, CORRELATION, now=NOW
        )
    changed.write_text("version = 2\n")

    result = executor(database, trusted, tmp_path / "data").execute(validation_job())
    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        rows = db.execute("SELECT diff_hash FROM change_sets ORDER BY rowid").fetchall()
        assert len(rows) == 2
        assert rows[0][0] == first.diff_hash
        assert rows[1][0] != first.diff_hash


def test_executor_uses_worker_owned_connection(tmp_path: Path) -> None:
    database, worktree, trusted = seed(tmp_path)
    (worktree / "safe.py").write_text("safe = True\n")
    connections: list[sqlite3.Connection] = []

    def connection_factory(path: Path) -> sqlite3.Connection:
        connection = open_database(path)
        connections.append(connection)
        return connection

    instance = ChangeValidationExecutor(
        database,
        trusted,
        tmp_path / "data",
        clock=lambda: NOW,
        connection_factory=connection_factory,
    )
    assert (
        instance.execute(validation_job()).disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
