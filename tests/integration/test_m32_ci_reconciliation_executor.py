"""M32.11 production-boundary coverage for the released M23 CI route."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.ci_scheduler import (
    CIJobCoordinator,
    CIReconciliationExecutor,
)
from syntra_build.application.lifecycle import STATE_JOBS
from syntra_build.application.production import ProductionDueWorkCoordinator
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    Scheduler,
    WorkerCapacity,
)
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
from syntra_build.domain.project_state_machine import ProjectTransitionRequest
from syntra_build.domain.projects import ProjectState
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence.ci import CIRunRecord, SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.migrations import apply_migrations
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)

NOW = datetime(2026, 10, 2, tzinfo=UTC)
HEAD = "a" * 40


def _seed(
    connection: sqlite3.Connection,
    name: str = "m32-ci",
    *,
    external_repository_id: int = 77,
) -> tuple[ProjectId, MilestoneId, str]:
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
            name,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
            name,
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
        VALUES (?,?,'github','owner',?,?,?,'public','main',
               'VERIFIED',?,?,?)""",
        (
            repository,
            str(project),
            name,
            f"owner/{name}",
            external_repository_id,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        external_repository_id,
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


class _DefaultMilestone:
    pass


_DEFAULT_MILESTONE = _DefaultMilestone()


def _job(
    project: ProjectId,
    milestone: MilestoneId,
    *,
    job_type: str = "CI_RECONCILE",
    worker_class: WorkerClass = WorkerClass.CI,
    milestone_id: MilestoneId | None | _DefaultMilestone = _DEFAULT_MILESTONE,
    payload: Mapping[str, str | int | float | bool | None] | None = None,
) -> Job:
    resolved_milestone = (
        milestone if isinstance(milestone_id, _DefaultMilestone) else milestone_id
    )
    return Job(
        JobId.generate(),
        project,
        job_type,
        JobState.QUEUED,
        0,
        NOW,
        NOW,
        resolved_milestone,
        correlation_id="m32.11",
        worker_class=worker_class,
        payload=payload,
    )


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
    def __init__(self, *, passing: bool = True) -> None:
        self.observations = 0
        self.passing = passing

    def observe(
        self, repository_full_name: str, pull_request_number: int, head_sha: str
    ) -> CIObservation:
        self.observations += 1
        assert head_sha == HEAD
        return CIObservation(
            (
                CICheck(
                    "test",
                    "check",
                    CICheckStatus.COMPLETED if self.passing else CICheckStatus.RUNNING,
                    CICheckConclusion.PASSED if self.passing else None,
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
    "job_factory",
    [
        lambda project, milestone: _job(project, milestone, job_type="PR_CREATE"),
        lambda project, milestone: _job(
            project, milestone, worker_class=WorkerClass.GITHUB
        ),
        lambda project, milestone: _job(project, milestone, milestone_id=None),
        lambda project, milestone: _job(project, milestone, payload={"head_sha": HEAD}),
    ],
)
def test_invalid_envelope_opens_no_worker_resources(
    tmp_path: Path, job_factory: Callable[[ProjectId, MilestoneId], Job]
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
        executor.execute(job_factory(project, milestone))
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


def _transition_project(
    connection: sqlite3.Connection,
    project: ProjectId,
    source: ProjectState,
    target: ProjectState,
    *,
    when: datetime = NOW,
) -> None:
    SQLiteProjectRepository(connection, lambda: str(uuid4())).apply_transition(
        ProjectTransitionRequest(
            project,
            source,
            target,
            "Pause/resume CI test",
            "HUMAN",
            "operator",
            "pause-resume-ci",
            when,
        )
    )


def _running_ci(
    connection: sqlite3.Connection,
    project: ProjectId,
    milestone: MilestoneId,
    pull_request_id: str,
) -> CIRunRecord:
    with transaction(connection):
        return SQLiteCIRepository(connection).reconcile(
            str(project),
            str(milestone),
            pull_request_id,
            HEAD,
            CIOverallStatus.RUNNING,
            (CICheck("test", "check", CICheckStatus.RUNNING),),
            ("run",),
            None,
            NOW,
            next_check_at=NOW - timedelta(seconds=1),
        )


def _ci_scheduler(
    connection: sqlite3.Connection,
    database: Path,
    project: ProjectId,
    milestone: MilestoneId,
    actions: Actions,
) -> Scheduler:
    executor = CIReconciliationExecutor(
        database,
        lambda connection: CIMonitor(
            connection, PullRequests(project, milestone), actions, clock=lambda: NOW
        ),
    )
    return Scheduler(
        SQLiteJobRepository(connection, lambda: str(uuid4())),
        WorkerCapacity(SchedulerConfig().worker_class_limits()),
        {WorkerClass.CI: executor},
        clock=lambda: NOW,
        due_work_enqueuer=ProductionDueWorkCoordinator(
            connection, clock=lambda: NOW
        ).enqueue_due,
    )


@pytest.mark.parametrize("existing_ci", [False, True], ids=["bootstrap", "due-poll"])
def test_paused_ci_scheduler_stays_idle_then_resumes_once(
    tmp_path: Path, existing_ci: bool
) -> None:
    database = tmp_path / "db"
    with open_database(database) as connection:
        apply_migrations(connection)
        project, milestone, pull_request_id = _seed(connection)
        record = (
            _running_ci(connection, project, milestone, pull_request_id)
            if existing_ci
            else None
        )
        original_milestone = tuple(
            connection.execute(
                "SELECT * FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()
        )
        original_pr = SQLitePullRequestRepository(connection).for_milestone(milestone)
        _transition_project(
            connection, project, ProjectState.BUILDING, ProjectState.PAUSED
        )
        actions = Actions(passing=False)
        scheduler = _ci_scheduler(connection, database, project, milestone, actions)
        try:
            for _ in range(3):
                cycle = scheduler.run_once()
                assert cycle.dispatched == 0
                assert cycle.errors == ()
            assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
            assert actions.observations == 0
            assert (
                tuple(
                    connection.execute(
                        "SELECT * FROM milestones WHERE id=?", (str(milestone),)
                    ).fetchone()
                )
                == original_milestone
            )
            assert (
                SQLitePullRequestRepository(connection).for_milestone(milestone)
                == original_pr
            )
            if record is not None:
                assert SQLiteCIRepository(connection).get(record.id) == record

            _transition_project(
                connection,
                project,
                ProjectState.PAUSED,
                ProjectState.BUILDING,
                when=NOW + timedelta(seconds=1),
            )
            assert scheduler.run_once().dispatched == 1
            assert scheduler.drain_until_idle(5)
            scheduler.exit_drain()
            for _ in range(3):
                cycle = scheduler.run_once()
                assert cycle.dispatched == 0
                assert cycle.errors == ()
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE job_type='CI_RECONCILE'"
            ).fetchall()
            assert len(jobs) == 1
            assert jobs[0]["state"] == "SUCCEEDED"
            assert jobs[0]["project_id"] == str(project)
            assert jobs[0]["milestone_id"] == str(milestone)
            assert actions.observations == 1
            current_ci = SQLiteCIRepository(connection).latest(str(milestone))
            assert current_ci is not None
            assert current_ci.overall_status is CIOverallStatus.RUNNING
            assert (
                current_ci.next_check_at is not None and current_ci.next_check_at > NOW
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM jobs WHERE worker_class='CODEX'"
                ).fetchone()[0]
                == 0
            )
            assert (
                SQLitePullRequestRepository(connection).for_milestone(milestone)
                == original_pr
            )
            projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
            assert [item.new_state for item in projects.transitions(project)] == [
                ProjectState.PAUSED,
                ProjectState.BUILDING,
            ]
        finally:
            scheduler.close()


@pytest.mark.parametrize("existing_ci", [False, True], ids=["bootstrap", "due-poll"])
def test_paused_ci_does_not_interrupt_other_building_projects(
    tmp_path: Path, existing_ci: bool
) -> None:
    database = tmp_path / "db"
    with open_database(database) as connection:
        apply_migrations(connection)
        paused, paused_milestone, paused_pr = _seed(
            connection, "paused", external_repository_id=78
        )
        building, building_milestone, building_pr = _seed(connection, "building")
        if existing_ci:
            _running_ci(connection, paused, paused_milestone, paused_pr)
            _running_ci(connection, building, building_milestone, building_pr)
        _transition_project(
            connection, paused, ProjectState.BUILDING, ProjectState.PAUSED
        )
        actions = Actions(passing=False)
        scheduler = _ci_scheduler(
            connection, database, building, building_milestone, actions
        )
        try:
            assert scheduler.run_once().dispatched == 1
            assert scheduler.drain_until_idle(5)
            scheduler.exit_drain()
            for _ in range(3):
                cycle = scheduler.run_once()
                assert cycle.dispatched == 0
                assert cycle.errors == ()
            assert (
                connection.execute(
                    "SELECT count(*) FROM jobs WHERE project_id=?", (str(paused),)
                ).fetchone()[0]
                == 0
            )
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE job_type='CI_RECONCILE'"
            ).fetchall()
            assert len(jobs) == 1
            assert jobs[0]["state"] == "SUCCEEDED"
            assert jobs[0]["project_id"] == str(building)
            assert jobs[0]["milestone_id"] == str(building_milestone)
            assert actions.observations == 1
            assert (
                connection.execute(
                    "SELECT state FROM milestones WHERE id=?", (str(paused_milestone),)
                ).fetchone()[0]
                == "CI_RUNNING"
            )
            assert (
                connection.execute(
                    "SELECT state FROM milestones WHERE id=?",
                    (str(building_milestone),),
                ).fetchone()[0]
                == "CI_RUNNING"
            )
            current_ci = SQLiteCIRepository(connection).latest(str(building_milestone))
            assert current_ci is not None
            assert current_ci.overall_status is CIOverallStatus.RUNNING
            assert (
                current_ci.next_check_at is not None and current_ci.next_check_at > NOW
            )
        finally:
            scheduler.close()


def test_due_ci_discovery_ignores_paused_project_then_resumes_once(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, pull_request_id = _seed(connection)
        record = _running_ci(connection, project, milestone, pull_request_id)
        _transition_project(
            connection, project, ProjectState.BUILDING, ProjectState.PAUSED
        )
        coordinator = CIJobCoordinator(connection)
        assert coordinator.enqueue_due(NOW) == 0
        assert coordinator.enqueue_due(NOW) == 0
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert SQLiteCIRepository(connection).get(record.id) == record
        _transition_project(
            connection, project, ProjectState.PAUSED, ProjectState.BUILDING
        )
        assert coordinator.enqueue_due(NOW) == 1
        assert coordinator.enqueue_due(NOW) == 0


def test_direct_bootstrap_still_rejects_paused_project(tmp_path: Path) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, _ = _seed(connection)
        _transition_project(
            connection, project, ProjectState.BUILDING, ProjectState.PAUSED
        )
        with pytest.raises(ValueError, match="CI bootstrap project must be BUILDING"):
            CIJobCoordinator(connection).bootstrap(project, milestone, "paused", NOW)
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
