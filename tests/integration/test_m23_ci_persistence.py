# ruff: noqa: E501
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, get_ident
from typing import cast
from uuid import uuid4

import pytest

from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.ci_policy import RequiredCheckPolicy
from syntra_build.application.ci_scheduler import (
    CIJobCoordinator,
    CIReconciliationExecutor,
)
from syntra_build.application.scheduler import Scheduler, WorkerCapacity
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIFailureClassification,
    CIObservation,
    CIOverallStatus,
)
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence.ci import SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
)
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)


def _seed(
    connection: sqlite3.Connection, name: str = "repo"
) -> tuple[ProjectId, MilestoneId, str, str]:
    now = datetime.now(UTC)
    project, milestone, repository = (
        ProjectId.generate(),
        MilestoneId.generate(),
        str(uuid4()),
    )
    external_repository_id = 77 if name in {"repo", "one"} else 78
    connection.execute(
        """INSERT INTO projects
        (id,name,state,resume_state,activity,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility)
        VALUES (?,?,'BUILDING',NULL,NULL,?,?,?,?, 'public')""",
        (str(project), name, now.isoformat(), now.isoformat(), now.isoformat(), name),
    )
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,resume_state,activity,started_at,completed_at,
         created_at,updated_at,codex_cycle_count,ci_rework_count,architect_rework_count,human_test_rework_count,
         exhaustion_reason,active_pull_request_id) VALUES (?,?,23,'M23','CI','CI_RUNNING',NULL,NULL,?,NULL,?,?,0,0,0,0,NULL,NULL)""",
        (
            str(milestone),
            str(project),
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    connection.execute(
        """INSERT INTO github_repositories VALUES
        (?,?,'github','owner',? ,?,?,'public','main','VERIFIED',?,?,?)""",
        (
            repository,
            str(project),
            name,
            f"owner/{name}",
            external_repository_id,
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        external_repository_id,
        12,
        PullRequestState.OPEN,
        "syntra/m23",
        "main",
        "a" * 40,
        "https://example/pr/12",
    )
    with transaction(connection):
        pr = SQLitePullRequestRepository(connection).save_verified(
            str(uuid4()), repository, descriptor, "M23", now
        )
    return project, milestone, repository, pr.id


def test_migrations_019_and_020_are_contiguous_and_upgrade_018(tmp_path: Path) -> None:
    assert MIGRATIONS[18].version == 19
    assert MIGRATIONS[18].name == "019_ci_monitoring"
    assert MIGRATIONS[19].version == 20
    assert MIGRATIONS[19].name == "020_ci_reconcile_job_uniqueness"
    with open_database(tmp_path / "clean.db") as connection:
        apply_migrations(connection)
        assert current_schema_version(connection) == len(MIGRATIONS)
    with open_database(tmp_path / "from-018.db") as connection:
        apply_migrations(connection, MIGRATIONS[:18])
        apply_migrations(connection)
        assert current_schema_version(connection) == len(MIGRATIONS)


def test_old_019_upgrades_to_020_and_rejects_duplicate_active_ci_jobs(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "from-old-019.db") as connection:
        apply_migrations(connection, MIGRATIONS[:19])
        assert current_schema_version(connection) == 19
        assert (
            connection.execute(
                """SELECT count(*) FROM sqlite_master WHERE type='index'
            AND name='one_active_ci_reconcile_per_milestone'"""
            ).fetchone()[0]
            == 0
        )
        project, milestone, _, _ = _seed(connection)
        now = datetime.now(UTC)
        jobs = SQLiteJobRepository(connection, lambda: str(uuid4()))
        jobs.add(
            Job(
                JobId.generate(),
                project,
                "CI_RECONCILE",
                JobState.QUEUED,
                0,
                now,
                now,
                milestone,
                correlation_id="first",
                worker_class=WorkerClass.CI,
            )
        )
        apply_migrations(connection, MIGRATIONS[:20])
        assert current_schema_version(connection) == 20
        assert (
            connection.execute(
                """SELECT count(*) FROM sqlite_master WHERE type='index'
            AND name='one_active_ci_reconcile_per_milestone'"""
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(PersistenceError):
            jobs.add(
                Job(
                    JobId.generate(),
                    project,
                    "CI_RECONCILE",
                    JobState.QUEUED,
                    0,
                    now,
                    now,
                    milestone,
                    correlation_id="duplicate",
                    worker_class=WorkerClass.CI,
                )
            )


def test_ci_runs_are_exact_sha_historical_and_checks_are_idempotent(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, _, pr_id = _seed(connection)
        records = SQLiteCIRepository(connection)
        check = CICheck("validate", "job-1", CICheckStatus.RUNNING)
        now = datetime.now(UTC)
        with transaction(connection):
            first = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.RUNNING,
                (check,),
                ("run-1",),
                None,
                now,
            )
        passed = CICheck(
            "validate", "job-1", CICheckStatus.COMPLETED, CICheckConclusion.PASSED
        )
        with transaction(connection):
            again = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.PASSED,
                (passed,),
                ("run-1",),
                None,
                now,
            )
            second = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "b" * 40,
                CIOverallStatus.UNKNOWN,
                (),
                (),
                None,
                now,
            )
        assert first.id == again.id and second.id != first.id
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM ci_checks").fetchone()[0] == 1
        assert records.get(first.id).overall_status is CIOverallStatus.PASSED


def test_ci_identity_rejects_cross_project_relationship(tmp_path: Path) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, _, pr_id = _seed(connection, "one")
        other_project, other_milestone, _, _ = _seed(connection, "two")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO ci_runs
                (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
                 overall_status,started_at,last_checked_at,summary_json)
                VALUES (?,?,?,?,?,1,'UNKNOWN',?,?,'{}')""",
                (
                    str(uuid4()),
                    str(other_project),
                    str(milestone),
                    pr_id,
                    "a" * 40,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        assert project != other_project and milestone != other_milestone


@pytest.mark.parametrize(
    ("first_status", "first_conclusion"),
    [
        (CIOverallStatus.FAILED, CICheckConclusion.FAILED),
        (CIOverallStatus.CANCELLED, CICheckConclusion.CANCELLED),
    ],
)
def test_same_sha_retry_preserves_terminal_attempt_then_passes(
    tmp_path: Path,
    first_status: CIOverallStatus,
    first_conclusion: CICheckConclusion,
) -> None:
    with open_database(tmp_path / f"{first_status}.db") as connection:
        apply_migrations(connection)
        project, milestone, _, pr_id = _seed(connection)
        records = SQLiteCIRepository(connection)
        now = datetime.now(UTC)
        terminal = CICheck(
            "validate", "old-job", CICheckStatus.COMPLETED, first_conclusion
        )
        with transaction(connection):
            old = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                first_status,
                (terminal,),
                ("run-1",),
                None,
                now,
            )
            queued = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.QUEUED,
                (),
                ("run-1",),
                None,
                now,
                retry_count=1,
                new_attempt=True,
            )
            current = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.PASSED,
                (
                    CICheck(
                        "validate",
                        "new-job",
                        CICheckStatus.COMPLETED,
                        CICheckConclusion.PASSED,
                    ),
                ),
                ("run-1",),
                None,
                now,
            )
        assert old.id != queued.id == current.id
        assert old.attempt_number == 1 and current.attempt_number == 2
        assert records.get(old.id).overall_status is first_status
        latest = records.latest_for_head(pr_id, "a" * 40)
        assert latest is not None and latest.overall_status is CIOverallStatus.PASSED


def test_due_running_ci_enqueues_lightweight_ci_job_but_terminal_stops(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    with open_database(tmp_path / "schedule.db") as connection:
        apply_migrations(connection)
        project, milestone, _, pr_id = _seed(connection)
        now = datetime.now(UTC)
        records = SQLiteCIRepository(connection)
        with transaction(connection):
            running = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.RUNNING,
                (CICheck("validate", "job", CICheckStatus.RUNNING),),
                ("run",),
                None,
                now,
                next_check_at=now - timedelta(seconds=1),
            )
        coordinator = CIJobCoordinator(connection, id_factory=lambda: str(uuid4()))
        assert coordinator.enqueue_due(now) == 1
        job = connection.execute(
            "SELECT * FROM jobs WHERE milestone_id=?", (str(milestone),)
        ).fetchone()
        assert job["worker_class"] == "CI" and job["job_type"] == "CI_RECONCILE"
        connection.execute("UPDATE jobs SET state='SUCCEEDED' WHERE id=?", (job["id"],))
        with transaction(connection):
            records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.PASSED,
                (
                    CICheck(
                        "validate",
                        "job",
                        CICheckStatus.COMPLETED,
                        CICheckConclusion.PASSED,
                    ),
                ),
                ("run",),
                None,
                now,
                next_check_at=None,
            )
        assert records.get(running.id).next_check_at is None
        assert coordinator.enqueue_due(now + timedelta(minutes=1)) == 0


def test_unit_test_failure_classification_enters_ci_rework(tmp_path: Path) -> None:
    from typing import cast

    from syntra_build.application.ci_monitor import CIActionsGateway
    from syntra_build.application.pull_requests import GitHubPullRequestGateway

    with open_database(tmp_path / "test-rework.db") as connection:
        apply_migrations(connection)
        project, milestone, _, pr_id = _seed(connection)
        now = datetime.now(UTC)
        failed_check = CICheck(
            "validate",
            "job",
            CICheckStatus.COMPLETED,
            CICheckConclusion.FAILED,
            failure_summary="Run unit tests",
        )
        classification = RequiredCheckPolicy().classify((failed_check,))
        with transaction(connection):
            record = SQLiteCIRepository(connection).reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.FAILED,
                (failed_check,),
                ("run",),
                classification,
                now,
            )
            monitor = CIMonitor(
                connection,
                cast(GitHubPullRequestGateway, object()),
                cast(CIActionsGateway, object()),
            )
            monitor._transition(record, project, milestone, "test-failure", now)
        assert classification is CIFailureClassification.TEST
        state = connection.execute(
            "SELECT state FROM milestones WHERE id=?", (str(milestone),)
        ).fetchone()[0]
        assert state == "CI_REWORK"


def test_monitor_reconciles_new_head_idempotently_and_advances_only_fresh_pass(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, _, _ = _seed(connection)

        class PullRequests:
            head = "a" * 40

            def get(
                self,
                repository_full_name: str,
                number: int,
                project_id: ProjectId,
                milestone_id: MilestoneId,
            ) -> PullRequestDescriptor:
                return PullRequestDescriptor(
                    "1.0",
                    project_id,
                    milestone_id,
                    77,
                    number,
                    PullRequestState.OPEN,
                    "syntra/m23",
                    "main",
                    self.head,
                    "https://example/pr/12",
                )

            def find_open(self, *args: object) -> tuple[()]:
                return ()

            def create(self, *args: object) -> PullRequestDescriptor:
                raise AssertionError("CI monitoring is read-only")

        class Actions:
            passing = False

            def observe(
                self,
                repository_full_name: str,
                pull_request_number: int,
                head_sha: str,
            ) -> CIObservation:
                check = (
                    CICheck(
                        "validate",
                        "job-1",
                        CICheckStatus.COMPLETED,
                        CICheckConclusion.PASSED,
                    )
                    if self.passing
                    else CICheck("validate", "job-1", CICheckStatus.RUNNING)
                )
                return CIObservation((check,), ("run-1",))

            def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
                raise AssertionError("no rerun expected")

        prs, actions = PullRequests(), Actions()
        monitor = CIMonitor(connection, prs, actions)
        first = monitor.reconcile(project, milestone, "correlation")
        repeated = monitor.reconcile(project, milestone, "correlation")
        assert first.id == repeated.id
        prs.head = "b" * 40
        actions.passing = True
        fresh = monitor.reconcile(project, milestone, "correlation")
        assert fresh.id != first.id and fresh.head_sha == "b" * 40
        assert (
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()[0]
            == "ARCHITECT_REVIEW"
        )
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 2


def test_monitor_closes_stale_head_race_before_transition(tmp_path: Path) -> None:
    with open_database(tmp_path / "race.db") as connection:
        apply_migrations(connection)
        project, milestone, _, _ = _seed(connection)

        class MovingPullRequest:
            calls = 0

            def get(
                self,
                repository_full_name: str,
                number: int,
                project_id: ProjectId,
                milestone_id: MilestoneId,
            ) -> PullRequestDescriptor:
                self.calls += 1
                head = "a" * 40 if self.calls == 1 else "b" * 40
                return PullRequestDescriptor(
                    "1.0",
                    project_id,
                    milestone_id,
                    77,
                    number,
                    PullRequestState.OPEN,
                    "syntra/m23",
                    "main",
                    head,
                    "https://example/pr/12",
                )

            def find_open(self, *args: object) -> tuple[()]:
                return ()

            def create(self, *args: object) -> PullRequestDescriptor:
                raise AssertionError("CI monitoring is read-only")

        class PassingActions:
            def observe(
                self,
                repository_full_name: str,
                pull_request_number: int,
                head_sha: str,
            ) -> CIObservation:
                assert pull_request_number == 12 and head_sha == "a" * 40
                return CIObservation(
                    (
                        CICheck(
                            "validate",
                            "job-a",
                            CICheckStatus.COMPLETED,
                            CICheckConclusion.PASSED,
                        ),
                    ),
                    ("run-a",),
                )

            def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
                raise AssertionError("no rerun expected")

        record = CIMonitor(connection, MovingPullRequest(), PassingActions()).reconcile(
            project, milestone, "race"
        )
        assert record.head_sha == "a" * 40
        milestone_row = connection.execute(
            "SELECT state FROM milestones WHERE id=?", (str(milestone),)
        ).fetchone()
        pr_row = connection.execute(
            "SELECT head_sha FROM pull_requests WHERE milestone_id=?", (str(milestone),)
        ).fetchone()
        assert milestone_row["state"] == "CI_RUNNING"
        assert pr_row["head_sha"] == "b" * 40


def test_expected_head_guard_precedes_any_ci_mutation(tmp_path: Path) -> None:
    from syntra_build.application.ci_monitor import CIMonitorError

    with open_database(tmp_path / "expected.db") as connection:
        apply_migrations(connection)
        project, milestone, _, _ = _seed(connection)

        class UnexpectedPullRequest:
            def get(
                self,
                repository_full_name: str,
                number: int,
                project_id: ProjectId,
                milestone_id: MilestoneId,
            ) -> PullRequestDescriptor:
                return PullRequestDescriptor(
                    "1.0",
                    project_id,
                    milestone_id,
                    77,
                    number,
                    PullRequestState.OPEN,
                    "syntra/m23",
                    "main",
                    "b" * 40,
                    "https://example/pr/12",
                )

            def find_open(self, *args: object) -> tuple[()]:
                return ()

            def create(self, *args: object) -> PullRequestDescriptor:
                raise AssertionError("CI monitoring is read-only")

        class UnusedActions:
            def observe(
                self, repository_full_name: str, pull_request_number: int, head_sha: str
            ) -> CIObservation:
                raise AssertionError("expected-head guard must precede Actions")

            def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
                raise AssertionError("expected-head guard must precede rerun")

        monitor = CIMonitor(connection, UnexpectedPullRequest(), UnusedActions())
        with pytest.raises(CIMonitorError, match="expected head"):
            monitor.reconcile(project, milestone, "smoke", expected_head_sha="a" * 40)
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 0
        state = connection.execute(
            "SELECT state FROM milestones WHERE id=?", (str(milestone),)
        ).fetchone()[0]
        assert state == "CI_RUNNING"


def test_terminal_transient_rerun_same_sha_can_pass(tmp_path: Path) -> None:
    from datetime import timedelta

    with open_database(tmp_path / "rerun.db") as connection:
        apply_migrations(connection)
        project, milestone, _, _ = _seed(connection)
        now = datetime.now(UTC)

        class StablePullRequest:
            def get(
                self,
                repository_full_name: str,
                number: int,
                project_id: ProjectId,
                milestone_id: MilestoneId,
            ) -> PullRequestDescriptor:
                return PullRequestDescriptor(
                    "1.0",
                    project_id,
                    milestone_id,
                    77,
                    number,
                    PullRequestState.OPEN,
                    "syntra/m23",
                    "main",
                    "a" * 40,
                    "https://example/pr/12",
                )

            def find_open(self, *args: object) -> tuple[()]:
                return ()

            def create(self, *args: object) -> PullRequestDescriptor:
                raise AssertionError("CI monitoring is read-only")

        class RetryActions:
            passing = False
            reruns: list[str] = []

            def observe(
                self, repository_full_name: str, pull_request_number: int, head_sha: str
            ) -> CIObservation:
                return CIObservation(
                    (
                        CICheck(
                            "validate",
                            "new-job" if self.passing else "old-job",
                            CICheckStatus.COMPLETED,
                            CICheckConclusion.PASSED
                            if self.passing
                            else CICheckConclusion.FAILED,
                            failure_summary=None
                            if self.passing
                            else "hosted runner lost",
                        ),
                    ),
                    ("run-1",),
                )

            def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
                self.reruns.append(workflow_run_id)
                self.passing = True

        actions = RetryActions()
        clock_value = [now]
        monitor = CIMonitor(
            connection, StablePullRequest(), actions, clock=lambda: clock_value[0]
        )
        failed = monitor.reconcile(project, milestone, "retry")
        assert failed.overall_status is CIOverallStatus.FAILED
        clock_value[0] += timedelta(seconds=5)
        queued = monitor.reconcile(project, milestone, "retry")
        assert queued.attempt_number == 2 and actions.reruns == ["run-1"]
        clock_value[0] += timedelta(seconds=20)
        passed = monitor.reconcile(project, milestone, "retry")
        assert passed.overall_status is CIOverallStatus.PASSED
        assert (
            SQLiteCIRepository(connection).get(failed.id).overall_status
            is CIOverallStatus.FAILED
        )


def test_scheduler_ci_executor_owns_worker_thread_database_connection(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "thread-owned.db"
    control_thread = get_ident()
    worker_threads: list[int] = []
    worker_closed = Event()
    with open_database(database_path) as control_connection:
        apply_migrations(control_connection)
        project, milestone, _, _ = _seed(control_connection)
        jobs = SQLiteJobRepository(control_connection, lambda: str(uuid4()))
        job_id = JobId.generate()
        now = datetime.now(UTC)
        jobs.add(
            Job(
                job_id,
                project,
                "CI_RECONCILE",
                JobState.QUEUED,
                0,
                now,
                now,
                milestone,
                correlation_id="ci-thread-test",
                worker_class=WorkerClass.CI,
            )
        )

        class StablePullRequest:
            def get(
                self,
                repository_full_name: str,
                number: int,
                project_id: ProjectId,
                milestone_id: MilestoneId,
            ) -> PullRequestDescriptor:
                return PullRequestDescriptor(
                    "1.0",
                    project_id,
                    milestone_id,
                    77,
                    number,
                    PullRequestState.OPEN,
                    "syntra/m23",
                    "main",
                    "a" * 40,
                    "https://example/pr/12",
                )

            def find_open(self, *args: object) -> tuple[()]:
                return ()

            def create(self, *args: object) -> PullRequestDescriptor:
                raise AssertionError("CI monitoring is read-only")

        class RunningActions:
            def observe(
                self,
                repository_full_name: str,
                pull_request_number: int,
                head_sha: str,
            ) -> CIObservation:
                return CIObservation(
                    (CICheck("validate", "job", CICheckStatus.RUNNING),),
                    ("run",),
                )

            def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
                raise AssertionError("no rerun expected")

        def monitor_factory(worker_connection: sqlite3.Connection) -> CIMonitor:
            worker_threads.append(get_ident())
            # This query would raise ProgrammingError if the control connection
            # had crossed into the ThreadPoolExecutor worker.
            worker_connection.execute("SELECT 1").fetchone()
            return CIMonitor(worker_connection, StablePullRequest(), RunningActions())

        class TrackedConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def __getattr__(self, name: str) -> object:
                return getattr(self.connection, name)

            def close(self) -> None:
                self.connection.close()
                worker_closed.set()

        def connection_factory(path: Path) -> sqlite3.Connection:
            return cast(sqlite3.Connection, TrackedConnection(open_database(path)))

        executor = CIReconciliationExecutor(
            database_path, monitor_factory, connection_factory
        )
        capacity = WorkerCapacity(SchedulerConfig().worker_class_limits())
        scheduler = Scheduler(
            jobs,
            capacity,
            {WorkerClass.CI: executor},
            clock=lambda: datetime.now(UTC),
        )
        try:
            assert scheduler.run_once().dispatched == 1
            scheduler.wait_for_wake(2)
            assert scheduler.run_once().completed == 1
            assert jobs.get(job_id).state is JobState.SUCCEEDED
            run = SQLiteCIRepository(control_connection).latest_for_head(
                control_connection.execute(
                    "SELECT id FROM pull_requests WHERE milestone_id=?",
                    (str(milestone),),
                ).fetchone()["id"],
                "a" * 40,
            )
            assert run is not None and run.overall_status is CIOverallStatus.RUNNING
            assert worker_threads and worker_threads[0] != control_thread
            assert worker_closed.is_set()
            assert capacity.in_use(WorkerClass.CODEX) == 0
            assert capacity.in_use(WorkerClass.CI) == 0
        finally:
            scheduler.close()
