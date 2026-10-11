"""Missing CI recovery is isolated, pause-safe, durable and revision-bound."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.application.ci_monitor import CIMonitor, CIMonitorError
from syntra_build.application.ci_polling import MissingCIPolicy
from syntra_build.application.ci_scheduler import (
    CIJobCoordinator,
    CIReconciliationExecutor,
)
from syntra_build.application.ci_status import format_ci_progress
from syntra_build.application.production import ProductionDueWorkCoordinator
from syntra_build.application.scheduler import Scheduler, WorkerCapacity
from syntra_build.application.status import SQLiteStatusService, format_project_status
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIDiagnosis,
    CIObservation,
    CIOverallStatus,
    CIWorkflowPresence,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.jobs import WorkerClass
from syntra_build.domain.milestones import Milestone, MilestoneState
from syntra_build.domain.project_state_machine import ProjectTransitionRequest
from syntra_build.domain.projects import Project, ProjectState
from syntra_build.domain.pull_requests import (
    PullRequestCreateRequest,
    PullRequestDescriptor,
    PullRequestState,
)
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence.ci import SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.migrations import apply_migrations
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)

NOW = datetime(2026, 10, 11, tzinfo=UTC)
HEAD = "a" * 40


@dataclass
class FrozenClock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


def _seed(
    connection: sqlite3.Connection, name: str, repository_id: int
) -> PullRequestDescriptor:
    project, milestone = ProjectId.generate(), MilestoneId.generate()
    projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
    projects.add(
        Project(project, name, ProjectState.BUILDING, NOW, NOW, canonical_name=name)
    )
    SQLiteMilestoneRepository(connection, lambda: str(uuid4())).add(
        Milestone(
            milestone, project, 1, "M01", "Tests", MilestoneState.CI_RUNNING, NOW, NOW
        )
    )
    repository = str(uuid4())
    connection.execute(
        """INSERT INTO github_repositories
        (id,project_id,provider,owner,repository_name,full_name,external_repository_id,
         visibility,default_branch,status,created_at,updated_at,verified_at)
        VALUES (?,?,'github','owner',?,?,?,'public','main','VERIFIED',?,?,?)""",
        (
            repository,
            str(project),
            name,
            f"owner/{name}",
            repository_id,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        repository_id,
        12,
        PullRequestState.OPEN,
        "syntra/m01",
        "main",
        HEAD,
        "https://example/pr/12",
    )
    with transaction(connection):
        SQLitePullRequestRepository(connection).save_verified(
            str(uuid4()), repository, descriptor, "Tests", NOW
        )
    return descriptor


class PullRequests:
    def __init__(self, descriptors: dict[str, PullRequestDescriptor]) -> None:
        self.descriptors = descriptors
        self.calls = 0

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        self.calls += 1
        descriptor = self.descriptors[repository_full_name]
        assert descriptor.project_id == project_id
        assert descriptor.milestone_id == milestone_id
        assert descriptor.pull_request_number == number
        return descriptor

    def find_open(
        self,
        repository_full_name: str,
        head_branch: str,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> tuple[PullRequestDescriptor, ...]:
        raise AssertionError("CI must not discover replacement PRs")

    def create(
        self, repository_full_name: str, request: PullRequestCreateRequest
    ) -> PullRequestDescriptor:
        raise AssertionError("CI must not create PRs")


class Actions:
    def __init__(self) -> None:
        self.passing: set[str] = set()
        self.running: set[str] = set()
        self.calls: dict[str, int] = {}

    def observe(
        self, repository_full_name: str, pull_request_number: int, head_sha: str
    ) -> CIObservation:
        assert pull_request_number == 12 and head_sha == HEAD
        self.calls[repository_full_name] = self.calls.get(repository_full_name, 0) + 1
        if repository_full_name in self.running:
            return CIObservation(
                (CICheck("test", "check", CICheckStatus.RUNNING),),
                ("run",),
                CIWorkflowPresence.CONFIGURED,
            )
        if repository_full_name in self.passing:
            return CIObservation(
                (
                    CICheck(
                        "test",
                        "check",
                        CICheckStatus.COMPLETED,
                        CICheckConclusion.PASSED,
                    ),
                ),
                ("run",),
                CIWorkflowPresence.CONFIGURED,
            )
        return CIObservation((), workflow_presence=CIWorkflowPresence.CONFIGURED)

    def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
        raise AssertionError("Missing CI infrastructure must not trigger a rerun")


def _monitor(
    connection: sqlite3.Connection,
    prs: PullRequests,
    actions: Actions,
    clock: FrozenClock,
) -> CIMonitor:
    return CIMonitor(
        connection,
        prs,
        actions,
        clock=clock,
        missing_ci=MissingCIPolicy(grace_seconds=60, max_observations=10),
    )


def _project_transition(
    connection: sqlite3.Connection,
    descriptor: PullRequestDescriptor,
    source: ProjectState,
    target: ProjectState,
    clock: FrozenClock,
) -> None:
    SQLiteProjectRepository(connection, lambda: str(uuid4())).apply_transition(
        ProjectTransitionRequest(
            descriptor.project_id,
            source,
            target,
            "Pause/resume regression",
            "HUMAN",
            "operator",
            "missing-ci",
            clock(),
        )
    )


@pytest.mark.parametrize(
    "delayed_pass", [False, True], ids=["still-missing", "delayed-pass"]
)
def test_missing_ci_pause_restart_resume_keeps_grace_and_other_projects_progress(
    tmp_path: Path,
    delayed_pass: bool,
) -> None:
    database = tmp_path / "ci.db"
    clock, actions = FrozenClock(), Actions()
    with open_database(database) as connection:
        apply_migrations(connection)
        paused = _seed(connection, "paused", 77)
        building = _seed(connection, "building", 78)
        prs = PullRequests({"owner/paused": paused, "owner/building": building})
        missing = _monitor(connection, prs, actions, clock).reconcile(
            paused.project_id, paused.milestone_id, "first", expected_head_sha=HEAD
        )
        assert missing.overall_status is CIOverallStatus.UNKNOWN
        assert missing.diagnosis is CIDiagnosis.NO_APPLICABLE_RUN
        assert missing.next_check_at is not None
        assert missing.unknown_deadline_at == NOW + timedelta(seconds=60)
        _project_transition(
            connection, paused, ProjectState.BUILDING, ProjectState.PAUSED, clock
        )
    clock.now = NOW + timedelta(seconds=120)
    # Fresh SQLite owners model a service restart while the project is paused.
    with open_database(database) as connection:
        records = SQLiteCIRepository(connection)
        assert records.get(missing.id) == missing
        coordinator = CIJobCoordinator(connection)
        assert coordinator.enqueue_due(clock()) == 0
        assert coordinator.enqueue_due(clock()) == 0
        original_calls = prs.calls
        with pytest.raises(CIMonitorError, match="BUILDING"):
            _monitor(connection, prs, actions, clock).reconcile(
                paused.project_id, paused.milestone_id, "paused", expected_head_sha=HEAD
            )
        assert prs.calls == original_calls
        assert actions.calls == {"owner/paused": 1}
        assert records.get(missing.id) == missing
        service = SQLiteStatusService(
            connection, SQLiteProjectRepository(connection, lambda: str(uuid4()))
        )
        assert service.project_status(paused.project_id).next_action.startswith(
            "Resume"
        )
        assert (
            f"CI grace deadline: {missing.unknown_deadline_at.isoformat()}"
            in format_project_status(service.project_status(paused.project_id))
        )
        actions.passing.add("owner/building")
        executor = CIReconciliationExecutor(
            database, lambda connection: _monitor(connection, prs, actions, clock)
        )
        scheduler = Scheduler(
            SQLiteJobRepository(connection, lambda: str(uuid4())),
            WorkerCapacity(SchedulerConfig().worker_class_limits()),
            {WorkerClass.CI: executor},
            clock=clock,
            due_work_enqueuer=ProductionDueWorkCoordinator(
                connection, clock=clock
            ).enqueue_due,
        )
        try:
            assert scheduler.run_once().dispatched == 1
            assert scheduler.drain_until_idle(5)
            scheduler.exit_drain()
            assert records.get(missing.id) == missing
            assert actions.calls == {"owner/paused": 1, "owner/building": 1}
            milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
            assert (
                milestones.get(building.milestone_id).state
                is MilestoneState.ARCHITECT_REVIEW
            )
            assert (
                milestones.get(paused.milestone_id).state is MilestoneState.CI_RUNNING
            )
            _project_transition(
                connection, paused, ProjectState.PAUSED, ProjectState.BUILDING, clock
            )
            if delayed_pass:
                actions.passing.add("owner/paused")
            assert coordinator.enqueue_due(clock()) == 1
            assert coordinator.enqueue_due(clock()) == 0
            assert scheduler.run_once().dispatched == 1
            assert scheduler.drain_until_idle(5)
            scheduler.exit_drain()
            current = records.get(missing.id)
            assert current.head_sha == HEAD
            assert current.last_checked_at == clock()
            assert current.next_check_at is None
            assert actions.calls == {"owner/paused": 2, "owner/building": 1}
            if delayed_pass:
                assert current.overall_status is CIOverallStatus.PASSED
                assert current.diagnosis is None
                assert (
                    milestones.get(paused.milestone_id).state
                    is MilestoneState.ARCHITECT_REVIEW
                )
            else:
                assert current.overall_status is CIOverallStatus.UNKNOWN
                assert current.unknown_since == missing.unknown_since
                assert current.unknown_deadline_at == missing.unknown_deadline_at
                assert current.unknown_observation_count == 2
                assert (
                    milestones.get(paused.milestone_id).state is MilestoneState.BLOCKED
                )
                status = service.project_status(paused.project_id)
                text = format_project_status(status)
                assert status.ci is not None and status.ci.current
                assert "NO_APPLICABLE_RUN" in text and HEAD in text
                assert "approved workflow" in status.next_action
                assert "triggers and filters" in status.next_action
                assert "GitHub Actions is enabled" in status.next_action
                assert "Human action: resolve CI blocker" in text
                assert "Human action: none" not in text
                assert status.latest_error == current.diagnosis_summary
            for _ in range(3):
                assert scheduler.run_once().dispatched == 0
            assert (
                connection.execute(
                    "SELECT count(*) FROM jobs WHERE worker_class='CODEX'"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM jobs WHERE job_type='CI_RECONCILE'"
                ).fetchone()[0]
                == 2
            )
            assert (
                len(milestones.transitions(paused.milestone_id, paused.project_id)) == 1
            )
        finally:
            scheduler.close()
    with open_database(database) as connection:
        assert SQLiteCIRepository(connection).get(missing.id) == current
        assert CIJobCoordinator(connection).enqueue_due(clock()) == 0
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 2


def test_stale_missing_ci_diagnosis_does_not_request_current_head_repair(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "status.db") as connection:
        apply_migrations(connection)
        descriptor = _seed(connection, "stale", 77)
        prs = PullRequests({"owner/stale": descriptor})
        actions, clock = Actions(), FrozenClock()
        first = _monitor(connection, prs, actions, clock).reconcile(
            descriptor.project_id,
            descriptor.milestone_id,
            "first",
            expected_head_sha=HEAD,
        )
        with transaction(connection):
            SQLiteCIRepository(connection).reconcile(
                str(descriptor.project_id),
                str(descriptor.milestone_id),
                first.pull_request_id,
                HEAD,
                CIOverallStatus.UNKNOWN,
                (),
                (),
                None,
                NOW,
                diagnosis=CIDiagnosis.NO_APPLICABLE_RUN,
                diagnosis_summary="No Actions runs for the old head",
            )
        connection.execute(
            "UPDATE pull_requests SET head_sha=? WHERE id=?",
            ("b" * 40, first.pull_request_id),
        )
        status = SQLiteStatusService(
            connection, SQLiteProjectRepository(connection, lambda: str(uuid4()))
        ).project_status(descriptor.project_id)
        text = format_project_status(status)
        assert status.ci is not None and not status.ci.current
        assert "stale; not current head" in text
        assert status.next_action == "Waiting for required CI checks."
        assert "Human action: resolve CI blocker" not in text


def test_missing_ci_marks_retained_running_checks_as_last_observed(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "history.db") as connection:
        apply_migrations(connection)
        descriptor = _seed(connection, "history", 77)
        prs = PullRequests({"owner/history": descriptor})
        actions, clock = Actions(), FrozenClock()
        actions.running.add("owner/history")
        monitor = _monitor(connection, prs, actions, clock)
        running = monitor.reconcile(
            descriptor.project_id,
            descriptor.milestone_id,
            "running",
            expected_head_sha=HEAD,
        )
        assert running.overall_status is CIOverallStatus.RUNNING
        assert running.diagnosis is None
        clock.now += timedelta(seconds=20)
        actions.running.clear()
        missing = monitor.reconcile(
            descriptor.project_id,
            descriptor.milestone_id,
            "missing",
            expected_head_sha=HEAD,
        )
        assert missing.overall_status is CIOverallStatus.UNKNOWN
        assert missing.diagnosis is CIDiagnosis.NO_APPLICABLE_RUN
        assert missing.checks == running.checks
        service = SQLiteStatusService(
            connection, SQLiteProjectRepository(connection, lambda: str(uuid4()))
        )
        for terminal in (False, True):
            if terminal:
                clock.now += timedelta(seconds=60)
                monitor.reconcile(
                    descriptor.project_id,
                    descriptor.milestone_id,
                    "expired",
                    expected_head_sha=HEAD,
                )
            text = format_project_status(service.project_status(descriptor.project_id))
            progress = format_ci_progress(
                monitor.progress(
                    descriptor.project_id,
                    descriptor.milestone_id,
                )
            )
            assert "Last observed checks:\n  test: RUNNING" in text
            assert "Last observed checks: test: RUNNING" in progress
            assert "NO_APPLICABLE_RUN" in text and "NO_APPLICABLE_RUN" in progress
            assert (
                "CI monitoring is blocked" in progress
                if terminal
                else "recheck at" in progress
            )


def test_due_unknown_current_head_is_not_starved_by_equal_clock_old_head(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "equal-clock.db") as connection:
        apply_migrations(connection)
        descriptor = _seed(connection, "equal-clock", 77)
        prs = SQLitePullRequestRepository(connection)
        pr = prs.for_milestone(descriptor.milestone_id)
        assert pr is not None
        records = SQLiteCIRepository(connection)
        with transaction(connection):
            old = records.reconcile(
                str(descriptor.project_id),
                str(descriptor.milestone_id),
                pr.id,
                HEAD,
                CIOverallStatus.UNKNOWN,
                (),
                (),
                None,
                NOW,
                next_check_at=NOW + timedelta(seconds=20),
                unknown_since=NOW,
                unknown_deadline_at=NOW + timedelta(seconds=60),
                unknown_observation_count=1,
                diagnosis=CIDiagnosis.NO_APPLICABLE_RUN,
            )
            prs.save_verified(
                pr.id,
                pr.github_repository_id,
                replace(descriptor, head_sha="b" * 40),
                pr.title,
                NOW,
            )
            current = records.reconcile(
                str(descriptor.project_id),
                str(descriptor.milestone_id),
                pr.id,
                "b" * 40,
                CIOverallStatus.UNKNOWN,
                (),
                (),
                None,
                NOW,
                next_check_at=NOW + timedelta(seconds=20),
                unknown_since=NOW,
                unknown_deadline_at=NOW + timedelta(seconds=60),
                unknown_observation_count=1,
                diagnosis=CIDiagnosis.NO_APPLICABLE_RUN,
            )
        assert old.started_at == current.started_at
        assert old.attempt_number == current.attempt_number == 1
        coordinator = CIJobCoordinator(connection)
        assert coordinator.enqueue_due(NOW + timedelta(seconds=21)) == 1
        assert coordinator.enqueue_due(NOW + timedelta(seconds=21)) == 0
        job = connection.execute("SELECT * FROM jobs").fetchone()
        assert job["project_id"] == str(descriptor.project_id)
        assert job["milestone_id"] == str(descriptor.milestone_id)
        assert job["worker_class"] == "CI" and job["job_type"] == "CI_RECONCILE"
        assert records.get(old.id) == old
        assert records.get(current.id) == current
