from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from test_m32_trusted_commit_executor import (
    CORRELATION,
    MID,
    NOW,
    PID,
    commit_job,
    git,
    milestone_state,
    prepare,
)
from test_m32_trusted_commit_executor import (
    executor as commit_executor,
)

from syntra_build.application.change_validation import ChangeValidationService
from syntra_build.application.m32_executors import TrustedPushExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain import Job, JobId, JobState, MilestoneState, WorkerClass
from syntra_build.domain.change_validation import ValidationDecision
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.workspaces import AmbiguousPushError
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import open_database


def push_job(**changes: object) -> Job:
    value = Job(
        JobId.from_string("00000000-0000-0000-0000-000000000706"),
        PID,
        "GIT_PUSH",
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


def ready(tmp_path: Path):  # type: ignore[no-untyped-def]
    database, worktree, trusted, _, _ = prepare(tmp_path)
    committed = commit_executor(database, trusted, tmp_path / "data").execute(
        commit_job()
    )
    assert committed.disposition is JobExecutionDisposition.SUCCEEDED
    return database, worktree, trusted


def subject(database: Path, trusted, data: Path) -> TrustedPushExecutor:  # type: ignore[no-untyped-def]
    return TrustedPushExecutor(database, trusted, data, clock=lambda: NOW)


def test_pushes_exact_trusted_head_and_advances(tmp_path: Path) -> None:
    database, _, trusted = ready(tmp_path)
    with open_database(database) as db:
        before = db.execute("SELECT * FROM commits").fetchone()
        managed = db.execute("SELECT * FROM git_repositories").fetchone()

    result = subject(database, trusted, tmp_path / "data").execute(push_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        after = db.execute("SELECT * FROM commits").fetchone()
        workspace = db.execute("SELECT * FROM git_workspaces").fetchone()
        assert after["pushed_at"] is not None
        for field in (
            "commit_sha",
            "parent_sha",
            "change_set_id",
            "validated_diff_hash",
            "message",
        ):
            assert after[field] == before[field]
        assert workspace["current_head_sha"] == after["commit_sha"]
        assert (
            trusted.remote_branch_sha(
                Path(managed["repository_path"]),
                managed["remote_url"],
                workspace["branch_name"],
            )
            == after["commit_sha"]
        )
    assert milestone_state(database) == MilestoneState.PR_CREATING.value


@pytest.mark.parametrize(
    "changes",
    [
        {"job_type": "GIT_COMMIT"},
        {"worker_class": WorkerClass.CODEX},
        {"milestone_id": None},
        {"payload": {"sha": "untrusted"}},
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

    executor = TrustedPushExecutor(
        tmp_path / "absent.db",
        object(),  # type: ignore[arg-type]
        tmp_path,
        connection_factory=connection_factory,
    )
    with pytest.raises(ValueError):
        executor.execute(push_job(**changes))
    assert not opened


def test_missing_commit_and_local_head_tamper_fail_closed(tmp_path: Path) -> None:
    database, worktree, trusted = ready(tmp_path)
    with open_database(database) as db:
        db.execute(
            "UPDATE git_workspaces SET current_head_sha=? WHERE milestone_id=?",
            ("f" * 40, str(MID)),
        )
        db.commit()
    missing = subject(database, trusted, tmp_path / "data").execute(push_job())
    assert missing.failure_classification is FailureClassification.PERMANENT
    assert milestone_state(database) == MilestoneState.PUSHING.value

    tamper_root = tmp_path / "tamper"
    tamper_root.mkdir()
    database, worktree, trusted = ready(tamper_root)
    git(worktree, "checkout", "--detach", "HEAD~1")
    tampered = subject(database, trusted, tamper_root / "data").execute(push_job())
    assert tampered.failure_classification is FailureClassification.PERMANENT
    with open_database(database) as db:
        assert db.execute("SELECT pushed_at FROM commits").fetchone()[0] is None
    assert milestone_state(database) == MilestoneState.PUSHING.value


def test_remote_already_applied_repairs_marker_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    database, _, trusted = ready(tmp_path)
    with open_database(database) as db:
        commit = db.execute("SELECT * FROM commits").fetchone()
        workspace = db.execute("SELECT * FROM git_workspaces").fetchone()
        managed = db.execute("SELECT * FROM git_repositories").fetchone()
    trusted.push(
        Path(managed["repository_path"]),
        managed["remote_url"],
        workspace["branch_name"],
    )

    result = subject(database, trusted, tmp_path / "data").execute(push_job())
    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        assert db.execute("SELECT pushed_at FROM commits").fetchone()[0] is not None
    assert milestone_state(database) == MilestoneState.PR_CREATING.value

    replay = subject(database, trusted, tmp_path / "data").execute(push_job())
    assert replay.disposition is JobExecutionDisposition.SUCCEEDED
    assert (
        trusted.remote_branch_sha(
            Path(managed["repository_path"]),
            managed["remote_url"],
            workspace["branch_name"],
        )
        == commit["commit_sha"]
    )


def test_durable_marker_with_changed_remote_fails_without_repair(
    tmp_path: Path,
) -> None:
    database, _, trusted = ready(tmp_path)
    first = subject(database, trusted, tmp_path / "data").execute(push_job())
    assert first.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        db.execute("UPDATE milestones SET state='PUSHING' WHERE id=?", (str(MID),))
        managed = db.execute("SELECT * FROM git_repositories").fetchone()
        workspace = db.execute("SELECT * FROM git_workspaces").fetchone()
        commit = db.execute("SELECT * FROM commits").fetchone()
        db.commit()
    remote = Path(managed["remote_url"])
    git(remote, "update-ref", "-d", f"refs/heads/{workspace['branch_name']}")

    result = subject(database, trusted, tmp_path / "data").execute(push_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.PERMANENT
    assert (
        trusted.remote_branch_sha(
            Path(managed["repository_path"]),
            managed["remote_url"],
            workspace["branch_name"],
        )
        is None
    )
    with open_database(database) as db:
        assert (
            db.execute("SELECT commit_sha FROM commits").fetchone()[0]
            == commit["commit_sha"]
        )
    assert milestone_state(database) == MilestoneState.PUSHING.value


class AmbiguousGit(TrustedGit):
    def __init__(self, auth_root: Path, outcome: str) -> None:
        super().__init__(auth_root)
        self.outcome = outcome
        self.push_calls = 0

    def push(self, repository: Path, remote_url: str, branch: str) -> None:
        self.push_calls += 1
        if self.outcome == "applied":
            super().push(repository, remote_url, branch)
        elif self.outcome == "third":
            remote = Path(remote_url)
            third_sha = git(remote, "rev-parse", "refs/heads/main")
            subprocess.run(
                ["git", "update-ref", f"refs/heads/{branch}", third_sha],
                cwd=remote,
                check=True,
                capture_output=True,
                text=True,
            )
        raise AmbiguousPushError("simulated ambiguous transport")


@pytest.mark.parametrize(
    ("outcome", "disposition", "classification", "state", "marked"),
    [
        ("applied", JobExecutionDisposition.SUCCEEDED, None, "PR_CREATING", True),
        (
            "unchanged",
            JobExecutionDisposition.FAILED,
            FailureClassification.TRANSIENT,
            "PUSHING",
            False,
        ),
        (
            "third",
            JobExecutionDisposition.FAILED,
            FailureClassification.PERMANENT,
            "PUSHING",
            False,
        ),
    ],
)
def test_ambiguous_push_is_reconciled_and_classified(
    tmp_path: Path,
    outcome: str,
    disposition: JobExecutionDisposition,
    classification: FailureClassification | None,
    state: str,
    marked: bool,
) -> None:
    database, _, _ = ready(tmp_path)
    ambiguous = AmbiguousGit(tmp_path / "data/auth", outcome)

    result = subject(database, ambiguous, tmp_path / "data").execute(push_job())

    assert result.disposition is disposition
    assert result.failure_classification is classification
    assert ambiguous.push_calls == 1
    with open_database(database) as db:
        assert (
            db.execute("SELECT pushed_at FROM commits").fetchone()[0] is not None
        ) is marked
    assert milestone_state(database) == state


def test_pr_creating_with_missing_push_marker_is_observational_only(
    tmp_path: Path,
) -> None:
    database, _, _ = ready(tmp_path)
    with open_database(database) as db:
        db.execute("UPDATE milestones SET state='PR_CREATING' WHERE id=?", (str(MID),))
        db.commit()
    ambiguous = AmbiguousGit(tmp_path / "data/auth", "applied")

    result = subject(database, ambiguous, tmp_path / "data").execute(push_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.PERMANENT
    assert ambiguous.push_calls == 0
    with open_database(database) as db:
        assert db.execute("SELECT pushed_at FROM commits").fetchone()[0] is None
        managed = db.execute("SELECT * FROM git_repositories").fetchone()
        workspace = db.execute("SELECT * FROM git_workspaces").fetchone()
    assert (
        ambiguous.remote_branch_sha(
            Path(managed["repository_path"]),
            managed["remote_url"],
            workspace["branch_name"],
        )
        is None
    )


def test_broken_validation_linkage_fails_before_remote_mutation(tmp_path: Path) -> None:
    database, _, _ = ready(tmp_path)
    with open_database(database) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER change_sets_no_delete")
        db.execute("DELETE FROM change_sets")
        db.commit()
    ambiguous = AmbiguousGit(tmp_path / "data/auth", "applied")

    result = subject(database, ambiguous, tmp_path / "data").execute(push_job())

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.PERMANENT
    assert ambiguous.push_calls == 0
    with open_database(database) as db:
        assert db.execute("SELECT pushed_at FROM commits").fetchone()[0] is None
    assert milestone_state(database) == MilestoneState.PUSHING.value


def test_current_head_selects_later_trusted_commit(tmp_path: Path) -> None:
    database, worktree, trusted = ready(tmp_path)
    first_push = subject(database, trusted, tmp_path / "data").execute(push_job())
    assert first_push.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        commit_a = db.execute("SELECT * FROM commits").fetchone()
        db.execute("UPDATE milestones SET state='COMMITTING' WHERE id=?", (str(MID),))
        db.commit()
    (worktree / "second.py").write_text("second = True\n")
    with open_database(database) as db:
        accepted = ChangeValidationService(db, trusted, tmp_path / "data").validate(
            PID, MID, "second-validation", now=NOW
        )
        db.commit()
    assert accepted.decision is ValidationDecision.ACCEPT
    committed = commit_executor(database, trusted, tmp_path / "data").execute(
        commit_job()
    )
    assert committed.disposition is JobExecutionDisposition.SUCCEEDED

    result = subject(database, trusted, tmp_path / "data").execute(push_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    with open_database(database) as db:
        commits = db.execute("SELECT * FROM commits ORDER BY created_at,id").fetchall()
        workspace = db.execute("SELECT * FROM git_workspaces").fetchone()
        managed = db.execute("SELECT * FROM git_repositories").fetchone()
        commit_b = next(row for row in commits if row["id"] != commit_a["id"])
        assert commit_a["pushed_at"] is not None
        assert commit_b["pushed_at"] is not None
        assert workspace["current_head_sha"] == commit_b["commit_sha"]
    assert (
        trusted.remote_branch_sha(
            Path(managed["repository_path"]),
            managed["remote_url"],
            workspace["branch_name"],
        )
        == commit_b["commit_sha"]
    )
    assert milestone_state(database) == MilestoneState.PR_CREATING.value


def test_executor_owns_and_closes_exactly_one_connection(tmp_path: Path) -> None:
    database, _, trusted = ready(tmp_path)
    opened: list[sqlite3.Connection] = []

    def connection_factory(path: Path) -> sqlite3.Connection:
        connection = open_database(path)
        opened.append(connection)
        return connection

    executor = TrustedPushExecutor(
        database,
        trusted,
        tmp_path / "data",
        clock=lambda: NOW,
        connection_factory=connection_factory,
    )

    result = executor.execute(push_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
