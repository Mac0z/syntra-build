"""M32.12 Scheduler boundary coverage for the released M24 review service."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pytest
from test_m24_architect_review import (
    NOW,
    SHA_A,
    ArchitectFake,
    GitHubFake,
    seeded,
)

from syntra_build.application.architect_review import ArchitectReviewService
from syntra_build.application.m32_executors import ArchitectReviewExecutor
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.reviews import ArchitectReviewVerdict
from syntra_build.infrastructure.persistence.connection import open_database


def _job(
    project: ProjectId,
    milestone: MilestoneId,
    *,
    correlation: str = "m32.12-review",
    job_type: str = "ARCHITECT_REVIEW",
    worker: WorkerClass = WorkerClass.ARCHITECT,
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
        correlation_id=correlation,
        worker_class=worker,
        payload=payload,
    )


def _prepared(
    tmp_path: Path,
    verdict: ArchitectReviewVerdict = ArchitectReviewVerdict.APPROVE,
    *,
    ci_sha: str | None = SHA_A,
    fail: bool = False,
) -> tuple[Path, ProjectId, MilestoneId, ArchitectFake, ArchitectReviewExecutor]:
    connection, project, milestone, _, descriptor = seeded(tmp_path, ci_sha=ci_sha)
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    connection.close()
    github, provider = GitHubFake(descriptor), ArchitectFake(verdict, fail=fail)
    executor = ArchitectReviewExecutor(
        database,
        lambda worker_connection: ArchitectReviewService(
            worker_connection,
            github,
            github,
            provider,
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
    )
    return database, project, milestone, provider, executor


def test_approve_persists_exact_head_then_replays_without_provider(
    tmp_path: Path,
) -> None:
    database, project, milestone, provider, executor = _prepared(tmp_path)
    job = _job(project, milestone)

    first = executor.execute(job)
    replay = executor.execute(job)

    assert first.disposition is JobExecutionDisposition.SUCCEEDED
    assert replay == first
    assert first.result is not None
    assert first.result["reviewed_sha"] == SHA_A
    assert first.result["verdict"] == "APPROVE"
    assert first.result["superseded"] is False
    assert len(provider.requests) == 1
    with open_database(database) as connection:
        assert (
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()[0]
            == MilestoneState.MERGE_READY.value
        )
        request = connection.execute(
            "SELECT status,correlation_id FROM architect_requests"
        ).fetchone()
        assert tuple(request) == ("SUCCEEDED", job.correlation_id)
        assert (
            connection.execute("SELECT count(*) FROM architect_reviews").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "change",
    [
        {"job_type": "CI_RECONCILE"},
        {"worker": WorkerClass.CI},
        {"payload": {"head_sha": SHA_A}},
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
    job = _job(project, milestone, **change)  # type: ignore[arg-type]
    executor = ArchitectReviewExecutor(
        tmp_path / "unused",
        lambda connection: pytest.fail("service constructed"),
        connection_factory=connection_factory,
    )
    with pytest.raises(ValueError):
        executor.execute(job)
    assert opened == 0


def test_missing_milestone_envelope_opens_no_resources(tmp_path: Path) -> None:
    opened = 0

    def connection_factory(path: Path) -> sqlite3.Connection:
        nonlocal opened
        opened += 1
        return open_database(path)

    project, milestone = ProjectId.generate(), MilestoneId.generate()
    job = _job(project, milestone)
    object.__setattr__(job, "milestone_id", None)
    executor = ArchitectReviewExecutor(
        tmp_path / "unused",
        lambda connection: pytest.fail("service constructed"),
        connection_factory=connection_factory,
    )
    with pytest.raises(ValueError):
        executor.execute(job)
    assert opened == 0


def test_missing_ci_and_late_new_correlation_fail_closed(tmp_path: Path) -> None:
    _, project, milestone, provider, executor = _prepared(tmp_path, ci_sha=None)
    missing_ci = executor.execute(_job(project, milestone))
    assert missing_ci.disposition is JobExecutionDisposition.FAILED
    assert missing_ci.failure_classification is FailureClassification.PERMANENT
    assert not provider.requests

    _, project, milestone, provider, executor = _prepared(tmp_path)
    assert (
        executor.execute(_job(project, milestone)).disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    late = executor.execute(_job(project, milestone, correlation="different"))
    assert late.disposition is JobExecutionDisposition.FAILED
    assert late.failure_classification is FailureClassification.PERMANENT
    assert len(provider.requests) == 1


def test_service_and_repositories_use_and_close_worker_connection(
    tmp_path: Path,
) -> None:
    database, project, milestone, _, base = _prepared(tmp_path)
    opened: list[sqlite3.Connection] = []

    def connection_factory(path: Path) -> sqlite3.Connection:
        connection = open_database(path)
        opened.append(connection)
        return connection

    executor = ArchitectReviewExecutor(
        database, base.service_factory, connection_factory=connection_factory
    )
    assert (
        executor.execute(_job(project, milestone)).disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_changes_required_stops_after_durable_rework_outcome(tmp_path: Path) -> None:
    database, project, milestone, provider, executor = _prepared(
        tmp_path, ArchitectReviewVerdict.CHANGES_REQUIRED
    )

    result = executor.execute(_job(project, milestone))

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert result.result is not None and result.result["finding_count"] == 1
    assert len(provider.requests) == 1
    with open_database(database) as connection:
        assert (
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()[0]
            # Released M24 coordination records REVIEW_REWORK and immediately
            # advances to CODING while enqueueing its durable (unexecuted) job.
            == MilestoneState.CODING.value
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM architect_rework_tasks"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM jobs WHERE job_type='CODEX_REVIEW_REWORK'"
            ).fetchone()[0]
            == 1
        )


def test_transient_provider_failure_is_classified_and_preserved(tmp_path: Path) -> None:
    database, project, milestone, provider, executor = _prepared(tmp_path, fail=True)

    result = executor.execute(_job(project, milestone))

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.TRANSIENT
    assert len(provider.requests) == 1
    with open_database(database) as connection:
        request = connection.execute(
            "SELECT status,failure_classification FROM architect_requests"
        ).fetchone()
        assert tuple(request) == ("FAILED", "TRANSIENT_PROVIDER")
        assert (
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()[0]
            == MilestoneState.ARCHITECT_REVIEW.value
        )
