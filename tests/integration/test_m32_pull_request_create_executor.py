from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_m32_trusted_commit_executor import CORRELATION, MID, NOW, PID
from test_m32_trusted_push_executor import push_job, ready

from syntra_build.application.m32_executors import (
    PullRequestCreateExecutor,
    TrustedPushExecutor,
)
from syntra_build.application.pull_requests import PullRequestLifecycleService
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain import Job, JobId, JobState, MilestoneState, WorkerClass
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.pull_requests import (
    PullRequestCreateRequest,
    PullRequestDescriptor,
    PullRequestState,
)
from syntra_build.infrastructure.persistence import open_database


class FakeGitHub:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.remote: list[PullRequestDescriptor] = []
        self.create_calls = 0
        self.get_calls = 0
        self.intent_seen = False

    def find_open(
        self,
        repository_full_name: str,
        head_branch: str,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> tuple[PullRequestDescriptor, ...]:
        return tuple(
            item for item in self.remote if item.state is PullRequestState.OPEN
        )

    def create(
        self, repository_full_name: str, request: PullRequestCreateRequest
    ) -> PullRequestDescriptor:
        self.create_calls += 1
        intent = self.connection.execute(
            "SELECT * FROM pull_request_creation_intents WHERE milestone_id=?",
            (str(request.milestone_id),),
        ).fetchone()
        self.intent_seen = (
            intent is not None
            and intent["head_sha"] == request.head_sha
            and intent["status"] == "RECONCILING"
        )
        created = PullRequestDescriptor(
            request.interface_version,
            request.project_id,
            request.milestone_id,
            request.repository_id,
            32,
            PullRequestState.OPEN,
            request.head_branch,
            request.base_branch,
            request.head_sha,
            f"https://github.com/{repository_full_name}/pull/32",
        )
        self.remote = [created]
        return created

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        self.get_calls += 1
        return next(item for item in self.remote if item.pull_request_number == number)


def pr_job(**changes: object) -> Job:
    value = Job(
        JobId.from_string("00000000-0000-0000-0000-000000000710"),
        PID,
        "PR_CREATE",
        JobState.RUNNING,
        0,
        NOW,
        NOW,
        MID,
        correlation_id=CORRELATION,
        max_attempts=3,
        worker_class=WorkerClass.GITHUB,
        payload={},
    )
    return replace(value, **changes)  # type: ignore[arg-type]


def prepared(tmp_path: Path):  # type: ignore[no-untyped-def]
    database, _, trusted = ready(tmp_path)
    pushed = TrustedPushExecutor(
        database, trusted, tmp_path / "data", clock=lambda: NOW
    ).execute(push_job())
    assert pushed.disposition is JobExecutionDisposition.SUCCEEDED
    holder: dict[str, FakeGitHub] = {}

    def factory(connection: sqlite3.Connection) -> PullRequestLifecycleService:
        github = holder.setdefault("github", FakeGitHub(connection))
        return PullRequestLifecycleService(
            connection,
            WorkspaceService(connection, trusted, tmp_path / "data"),
            github,
        )

    return (
        database,
        trusted,
        holder,
        PullRequestCreateExecutor(database, factory, clock=lambda: NOW),
    )


def test_creates_exact_pr_persists_evidence_and_advances_without_ci_job(
    tmp_path: Path,
) -> None:
    database, _, holder, executor = prepared(tmp_path)

    result = executor.execute(pr_job())

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    github = holder["github"]
    assert github.intent_seen and github.create_calls == 1 and github.get_calls == 2
    with open_database(database) as connection:
        workspace = connection.execute("SELECT * FROM git_workspaces").fetchone()
        commit = connection.execute("SELECT * FROM commits").fetchone()
        pull_request = connection.execute("SELECT * FROM pull_requests").fetchone()
        milestone = connection.execute("SELECT * FROM milestones").fetchone()
        assert pull_request["state"] == PullRequestState.OPEN
        assert pull_request["head_sha"] == workspace["current_head_sha"]
        assert pull_request["head_sha"] == commit["commit_sha"]
        assert pull_request["head_branch"] == workspace["branch_name"]
        assert pull_request["base_branch"] == "main"
        assert milestone["active_pull_request_id"] == pull_request["id"]
        assert milestone["state"] == MilestoneState.CI_RUNNING
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_type='CI_RECONCILE'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"job_type": "GIT_PUSH"},
        {"worker_class": WorkerClass.GIT},
        {"milestone_id": None},
        {"payload": {"head": "untrusted"}},
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

    executor = PullRequestCreateExecutor(
        tmp_path / "absent.db",
        lambda connection: pytest.fail("service factory called"),
        connection_factory=connection_factory,
    )
    with pytest.raises(ValueError):
        executor.execute(pr_job(**changes))
    assert not opened


def test_missing_push_and_remote_mismatch_fail_before_pr_mutation(
    tmp_path: Path,
) -> None:
    database, _, holder, executor = prepared(tmp_path)
    with open_database(database) as connection:
        connection.execute("UPDATE commits SET pushed_at=NULL")
        connection.commit()
    missing = executor.execute(pr_job())
    assert missing.failure_classification is FailureClassification.PERMANENT
    assert "github" not in holder

    other = tmp_path / "remote-mismatch"
    other.mkdir()
    database, _, holder, executor = prepared(other)
    with open_database(database) as connection:
        managed = connection.execute("SELECT * FROM git_repositories").fetchone()
        workspace = connection.execute("SELECT * FROM git_workspaces").fetchone()
    # Make the milestone remote ref disappear without invoking any executor push.
    remote = Path(managed["remote_url"].removeprefix("file://"))
    import subprocess

    subprocess.run(
        ["git", "update-ref", "-d", f"refs/heads/{workspace['branch_name']}"],
        cwd=remote,
        check=True,
    )
    mismatch = executor.execute(pr_job())
    assert mismatch.failure_classification is FailureClassification.PERMANENT
    assert holder["github"].create_calls == 0


def test_persisted_and_ci_running_replays_are_observational(tmp_path: Path) -> None:
    database, _, holder, executor = prepared(tmp_path)
    assert executor.execute(pr_job()).disposition is JobExecutionDisposition.SUCCEEDED
    github = holder["github"]
    with open_database(database) as connection:
        connection.execute("UPDATE milestones SET state='PR_CREATING'")
        connection.commit()

    repaired = executor.execute(pr_job())
    assert repaired.disposition is JobExecutionDisposition.SUCCEEDED
    assert github.create_calls == 1
    replayed = executor.execute(pr_job())
    assert replayed.disposition is JobExecutionDisposition.SUCCEEDED
    assert github.create_calls == 1
    with open_database(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_type='CI_RECONCILE'"
            ).fetchone()[0]
            == 0
        )


def test_ci_running_without_durable_pr_does_not_reconstruct(tmp_path: Path) -> None:
    database, _, holder, executor = prepared(tmp_path)
    with open_database(database) as connection:
        connection.execute("UPDATE milestones SET state='CI_RUNNING'")
        connection.commit()

    result = executor.execute(pr_job())

    assert result.failure_classification is FailureClassification.PERMANENT
    assert holder["github"].create_calls == 0
