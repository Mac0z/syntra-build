"""M32.11 production-boundary coverage for the released M23 CI route."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.ci_scheduler import (
    CIJobCoordinator,
    CIReconciliationExecutor,
)
from syntra_build.application.lifecycle import STATE_JOBS
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIObservation,
    CIOverallStatus,
)
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.infrastructure.persistence.ci import SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.migrations import apply_migrations
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)

NOW = datetime(2026, 10, 2, tzinfo=UTC)
HEAD = "a" * 40


def _seed(connection: sqlite3.Connection) -> tuple[ProjectId, MilestoneId, str]:
    project, milestone, repository = (
        ProjectId.generate(),
        MilestoneId.generate(),
        str(uuid4()),
    )
    connection.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility)
        VALUES (?,?,'BUILDING',?,?,?,?, 'public')""",
        (
            str(project),
            "m32-ci",
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
            "m32-ci",
        ),
    )
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,started_at,created_at,updated_at)
        VALUES (?,?,32,'M32.11','CI route','CI_RUNNING',?,?,?)""",
        (
            str(milestone),
            str(project),
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    connection.execute(
        """INSERT INTO github_repositories
        (id,project_id,provider,owner,repository_name,full_name,external_repository_id,
         visibility,default_branch,status,created_at,updated_at,verified_at)
        VALUES (?,?,'github','owner','repo','owner/repo',77,'public','main',
               'VERIFIED',?,?,?)""",
        (repository, str(project), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        77,
        12,
        PullRequestState.OPEN,
        "syntra/m32-11",
        "main",
        HEAD,
        "https://example/pr/12",
    )
    with transaction(connection):
        pull_request = SQLitePullRequestRepository(connection).save_verified(
            str(uuid4()), repository, descriptor, "M32.11", NOW
        )
    return project, milestone, pull_request.id


def _job(project: ProjectId, milestone: MilestoneId, **changes: object) -> Job:
    values: dict[str, object] = {
        "job_type": "CI_RECONCILE",
        "worker_class": WorkerClass.CI,
        "milestone_id": milestone,
        "payload": None,
    }
    values.update(changes)
    return Job(
        JobId.generate(),
        project,
        str(values["job_type"]),
        JobState.QUEUED,
        0,
        NOW,
        NOW,
        values["milestone_id"],
        correlation_id="m32.11",
        worker_class=values["worker_class"],
        payload=values["payload"],
    )  # type: ignore[arg-type]


class PullRequests:
    def __init__(
        self,
        project: ProjectId,
        milestone: MilestoneId,
        heads: tuple[str, ...] = (HEAD, HEAD),
    ) -> None:
        self.project, self.milestone, self.heads = project, milestone, list(heads)

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        return PullRequestDescriptor(
            "1.0",
            self.project,
            self.milestone,
            77,
            12,
            PullRequestState.OPEN,
            "syntra/m32-11",
            "main",
            self.heads.pop(0),
            "https://example/pr/12",
        )

    def find_open(self, *args: object) -> tuple[()]:
        return ()

    def create(self, *args: object) -> PullRequestDescriptor:
        raise AssertionError("CI route must not create pull requests")


class Actions:
    def __init__(self) -> None:
        self.observations = 0

    def observe(
        self, repository_full_name: str, pull_request_number: int, head_sha: str
    ) -> CIObservation:
        self.observations += 1
        assert head_sha == HEAD
        return CIObservation(
            (
                CICheck(
                    "test", "check", CICheckStatus.COMPLETED, CICheckConclusion.PASSED
                ),
            ),
            ("run",),
        )

    def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
        raise AssertionError("passing CI must not rerun")


def test_pass_uses_exact_persisted_head_and_stops_at_architect_review(
    tmp_path: Path,
) -> None:
    database = tmp_path / "db"
    with open_database(database) as connection:
        apply_migrations(connection)
        project, milestone, pull_request_id = _seed(connection)
    actions = Actions()
    executor = CIReconciliationExecutor(
        database,
        lambda connection: CIMonitor(
            connection, PullRequests(project, milestone), actions, clock=lambda: NOW
        ),
    )

    result = executor.execute(_job(project, milestone))

    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert result.result is not None
    assert result.result["head_sha"] == HEAD
    assert result.result["attempt_number"] == 1
    with open_database(database) as connection:
        record = SQLiteCIRepository(connection).get(str(result.result["ci_run_id"]))
        assert record.pull_request_id == pull_request_id
        assert record.head_sha == HEAD
        assert record.overall_status is CIOverallStatus.PASSED
        assert (
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()[0]
            == "ARCHITECT_REVIEW"
        )
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"job_type": "PR_CREATE"},
        {"worker_class": WorkerClass.GITHUB},
        {"milestone_id": None},
        {"payload": {"head_sha": HEAD}},
    ],
)
def test_invalid_envelope_opens_no_worker_resources(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    opened = 0
    project, milestone = ProjectId.generate(), MilestoneId.generate()

    def connection_factory(path: Path) -> sqlite3.Connection:
        nonlocal opened
        opened += 1
        return open_database(path)

    executor = CIReconciliationExecutor(
        tmp_path / "unused",
        lambda connection: pytest.fail("monitor constructed"),
        connection_factory,
    )
    with pytest.raises(ValueError):
        executor.execute(_job(project, milestone, **changes))
    assert opened == 0


def test_stale_head_race_keeps_history_but_fails_scheduler_execution(
    tmp_path: Path,
) -> None:
    database = tmp_path / "db"
    with open_database(database) as connection:
        apply_migrations(connection)
        project, milestone, pull_request_id = _seed(connection)
    executor = CIReconciliationExecutor(
        database,
        lambda connection: CIMonitor(
            connection,
            PullRequests(project, milestone, (HEAD, "b" * 40)),
            Actions(),
            clock=lambda: NOW,
        ),
    )

    result = executor.execute(_job(project, milestone))

    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.failure_classification is FailureClassification.PERMANENT
    with open_database(database) as connection:
        historical = SQLiteCIRepository(connection).latest_for_head(
            pull_request_id, HEAD
        )
        assert (
            historical is not None
            and historical.overall_status is CIOverallStatus.PASSED
        )
        assert (
            SQLiteCIRepository(connection).latest_for_head(pull_request_id, "b" * 40)
            is None
        )
        assert (
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()[0]
            == "CI_RUNNING"
        )


def test_bootstrap_is_idempotent_and_generic_lifecycle_does_not_schedule_ci(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, _ = _seed(connection)
        coordinator = CIJobCoordinator(connection, id_factory=lambda: str(uuid4()))
        assert coordinator.bootstrap(project, milestone, "first", NOW)
        assert not coordinator.bootstrap(project, milestone, "second", NOW)
        row = connection.execute("SELECT * FROM jobs").fetchone()
        assert row["job_type"] == "CI_RECONCILE"
        assert row["worker_class"] == "CI"
        assert row["milestone_id"] == str(milestone)
        assert row["payload_json"] == "{}"
    assert "CI_RUNNING" not in STATE_JOBS
