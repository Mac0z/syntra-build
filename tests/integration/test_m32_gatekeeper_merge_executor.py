"""M32.14 Scheduler boundary coverage for the released M26 Gatekeeper."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from test_m26_gatekeeper import MERGE_SHA, NOW, SHA, FakeGitHub, Seed, seed_eligible

from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.application.m32_executors import GatekeeperMergeExecutor
from syntra_build.application.provisioning import AmbiguousGitHubResult
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.pull_requests import PullRequestState
from syntra_build.infrastructure.persistence import apply_migrations, open_database


def job(
    project: ProjectId,
    milestone: MilestoneId,
    *,
    job_type: str = "PR_MERGE",
    worker: WorkerClass = WorkerClass.GITHUB,
    milestone_id: MilestoneId | None = None,
    payload: Mapping[str, str] | None = None,
) -> Job:
    return Job(
        JobId.generate(),
        project,
        job_type,
        JobState.QUEUED,
        0,
        NOW,
        NOW,
        milestone if milestone_id is None else milestone_id,
        correlation_id="m32.14-merge",
        worker_class=worker,
        payload=payload,
    )


def prepared(
    tmp_path: Path, *, ci_status: str | None = "PASSED"
) -> tuple[Path, Seed, FakeGitHub, GatekeeperMergeExecutor]:
    database = tmp_path / "merge.db"
    with open_database(database) as connection:
        apply_migrations(connection)
        seed, github = seed_eligible(connection, ci_status=ci_status)
    executor = GatekeeperMergeExecutor(
        database,
        lambda connection: Gatekeeper(connection, github),
        clock=lambda: NOW,
    )
    return database, seed, github, executor


def merged(github: FakeGitHub) -> None:
    github.live = replace(
        github.live,
        state=PullRequestState.MERGED,
        merged_at=NOW.isoformat(),
        merge_commit_sha=MERGE_SHA,
    )


def test_happy_path_persists_intent_before_one_put_and_independently_verifies(
    tmp_path: Path,
) -> None:
    database, seed, github, executor = prepared(tmp_path)

    def assert_durable_intent() -> None:
        with open_database(database) as observation:
            attempt = observation.execute("SELECT * FROM merge_attempts").fetchone()
            assert attempt is not None
            assert attempt["status"] == "REQUESTED"
            assert attempt["mutation_started_at"] == NOW.isoformat()
            assert (
                observation.execute(
                    "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
                ).fetchone()[0]
                == "MERGING"
            )
        merged(github)

    github.before_merge = assert_durable_intent
    result = executor.execute(job(seed.project, seed.milestone))

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert result.result is not None
    assert result.result == {
        "merge_attempt_id": result.result["merge_attempt_id"],
        "pull_request_id": seed.pr,
        "pull_request_number": 7,
        "expected_head_sha": SHA,
        "merge_status": "MERGED",
        "merge_commit_sha": MERGE_SHA,
    }
    assert len(github.merge_calls) == 1
    assert len(github.get_calls) >= 4  # evaluate, pre-PUT, reconciliation/verify
    with open_database(database) as connection:
        assert (
            connection.execute(
                "SELECT eligible FROM merge_eligibility_results"
            ).fetchone()[0]
            == 1
        )
        assert tuple(
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
            ).fetchone()
        ) == ("COMPLETE",)
        assert (
            connection.execute(
                "SELECT state FROM projects WHERE id=?", (str(seed.project),)
            ).fetchone()[0]
            == "BUILDING"
        )
        assert tuple(
            connection.execute(
                "SELECT status,merge_commit_sha FROM merge_attempts"
            ).fetchone()
        ) == ("MERGED", MERGE_SHA)


@pytest.mark.parametrize(
    "change",
    [
        {"job_type": "ARCHITECT_REVIEW"},
        {"worker": WorkerClass.CI},
        {"milestone_id": None},
        {"payload": {"pull_request_number": "7"}},
    ],
)
def test_invalid_envelope_opens_no_resources(
    tmp_path: Path, change: dict[str, object]
) -> None:
    opened = 0

    def connection_factory(path: Path) -> sqlite3.Connection:
        nonlocal opened
        opened += 1
        return open_database(path)

    project, milestone = ProjectId.generate(), MilestoneId.generate()
    candidate = job(project, milestone, **change)  # type: ignore[arg-type]
    if "milestone_id" in change:
        candidate = replace(candidate, milestone_id=None)
    executor = GatekeeperMergeExecutor(
        tmp_path / "unused",
        lambda connection: pytest.fail("Gatekeeper constructed"),
        connection_factory=connection_factory,
    )
    with pytest.raises(ValueError):
        executor.execute(candidate)
    assert opened == 0


def test_policy_rejection_is_non_retryable_and_does_not_mutate(tmp_path: Path) -> None:
    database, seed, github, executor = prepared(tmp_path, ci_status=None)
    result = executor.execute(job(seed.project, seed.milestone))
    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.POLICY
    assert github.merge_calls == []
    with open_database(database) as connection:
        assert (
            connection.execute("SELECT count(*) FROM merge_attempts").fetchone()[0] == 0
        )
        assert (
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(seed.milestone),)
            ).fetchone()[0]
            == "MERGE_READY"
        )


def test_ambiguous_put_unresolved_is_never_replayed(tmp_path: Path) -> None:
    database, seed, github, executor = prepared(tmp_path)
    github.merge_result = AmbiguousGitHubResult("lost response")
    first = executor.execute(job(seed.project, seed.milestone))
    second = executor.execute(job(seed.project, seed.milestone))
    assert first.failure_classification is FailureClassification.TRANSIENT
    assert second.failure_classification is FailureClassification.TRANSIENT
    assert len(github.merge_calls) == 1
    with open_database(database) as connection:
        assert (
            connection.execute("SELECT status FROM merge_attempts").fetchone()[0]
            == "UNKNOWN"
        )
        assert connection.execute(
            "SELECT mutation_started_at FROM merge_attempts"
        ).fetchone()[0]


def test_prepared_without_put_can_execute_but_started_attempt_only_recovers(
    tmp_path: Path,
) -> None:
    database, seed, github, executor = prepared(tmp_path)
    with open_database(database) as connection:
        attempt, _request = Gatekeeper(connection, github).prepare(
            seed.request, now=NOW
        )
    merged(github)
    recovered = executor.execute(job(seed.project, seed.milestone))
    assert recovered.disposition is JobExecutionDisposition.SUCCEEDED
    assert github.merge_calls == []

    # COMPLETE replay is entirely observational and preserves project state.
    replay = executor.execute(job(seed.project, seed.milestone))
    assert replay == recovered
    assert github.merge_calls == []
    with open_database(database) as connection:
        assert (
            connection.execute(
                "SELECT id FROM merge_attempts WHERE id=?", (attempt,)
            ).fetchone()
            is not None
        )
        assert (
            connection.execute(
                "SELECT state FROM projects WHERE id=?", (str(seed.project),)
            ).fetchone()[0]
            == "BUILDING"
        )


def test_worker_connection_is_single_owned_and_closed(tmp_path: Path) -> None:
    database, seed, github, _executor = prepared(tmp_path)
    connections: list[sqlite3.Connection] = []

    def factory(path: Path) -> sqlite3.Connection:
        connection = open_database(path)
        connections.append(connection)
        return connection

    def keeper_factory(connection: sqlite3.Connection) -> Gatekeeper:
        assert connection is connections[0]
        return Gatekeeper(connection, github)

    github.before_merge = lambda: merged(github)
    executor = GatekeeperMergeExecutor(
        database, keeper_factory, clock=lambda: NOW, connection_factory=factory
    )
    assert (
        executor.execute(job(seed.project, seed.milestone)).disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")
