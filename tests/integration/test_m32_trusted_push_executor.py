from __future__ import annotations

import sqlite3
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

from syntra_build.application.m32_executors import TrustedPushExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain import Job, JobId, JobState, MilestoneState, WorkerClass
from syntra_build.domain.failures import FailureClassification
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
