# mypy: disable-error-code="no-untyped-def"
"""M32.19 acceptance: restart recovery restores, but does not execute, M32 routes."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from test_m24_architect_review import (
    SHA_B,
)
from test_m24_architect_review import (
    ArchitectFake as StaleArchitect,
)
from test_m24_architect_review import (
    GitHubFake as StaleGitHub,
)
from test_m24_architect_review import (
    seeded as seed_review,
)
from test_m26_gatekeeper import MERGE_SHA, seed_eligible
from test_m26_gatekeeper import NOW as MERGE_NOW
from test_m32_controlled_rework_path import (
    MILESTONE as REWORK_MILESTONE,
)
from test_m32_controlled_rework_path import (
    PROJECT as REWORK_PROJECT,
)
from test_m32_controlled_rework_path import (
    FakeActions as ReworkActions,
)
from test_m32_controlled_rework_path import (
    FakeArchitect as ReworkArchitect,
)
from test_m32_controlled_rework_path import (
    FakeCodexProcess as ReworkCodexProcess,
)
from test_m32_controlled_rework_path import (
    FakeGitHub as ReworkGitHub,
)
from test_m32_controlled_rework_path import (
    LocalRemoteTrustedGit as ReworkTrustedGit,
)
from test_m32_controlled_rework_path import (
    config as rework_config,
)
from test_m32_controlled_rework_path import (
    git as rework_git,
)
from test_m32_controlled_rework_path import (
    seed as seed_rework_path,
)
from test_m32_one_milestone_happy_path import (
    EXPECTED_JOBS,
    FakeActions,
    FakeArchitect,
    FakeCodexProcess,
    FakeGitHub,
    LocalRemoteTrustedGit,
    diagnostic,
)
from test_m32_one_milestone_happy_path import (
    MILESTONE as HAPPY_MILESTONE,
)
from test_m32_one_milestone_happy_path import PROJECT as HAPPY_PROJECT
from test_m32_one_milestone_happy_path import (
    config as happy_config,
)
from test_m32_one_milestone_happy_path import (
    seed as seed_happy_path,
)

from syntra_build.application.architect import ArchitectProvider
from syntra_build.application.architect_review import ArchitectReviewService
from syntra_build.application.codex import WorkspaceBoundCodexRunner
from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.application.m32_executors import ArchitectReviewExecutor
from syntra_build.application.production import (
    ProductionDependencies,
    ProductionDueWorkCoordinator,
    build_production_executors,
)
from syntra_build.application.recovery import RecoveryCoordinator, RecoveryServices
from syntra_build.application.scheduler import JobExecutor, Scheduler, WorkerCapacity
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain import (
    Job,
    JobId,
    JobState,
    JobTransitionRequest,
    Milestone,
    MilestoneId,
    MilestoneState,
    Project,
    ProjectId,
    ProjectState,
    WorkerClass,
)
from syntra_build.domain.codex import CodexRunRequest
from syntra_build.domain.merges import MergeStrategy
from syntra_build.domain.pull_requests import PullRequestState
from syntra_build.domain.reviews import ArchitectReviewVerdict
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence import apply_migrations, open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def _scheduler(connection, executors=None) -> Scheduler:
    return Scheduler(
        SQLiteJobRepository(connection, lambda: str(uuid4())),
        WorkerCapacity(SchedulerConfig().worker_class_limits()),
        executors or {},
    )


def _seed(path: Path, state: MilestoneState):
    connection = open_database(path)
    apply_migrations(connection)
    project, milestone = ProjectId.generate(), MilestoneId.generate()
    SQLiteProjectRepository(connection, lambda: str(uuid4())).add(
        Project(project, "M32.19", ProjectState.BUILDING, NOW, NOW)
    )
    SQLiteMilestoneRepository(connection, lambda: str(uuid4())).add(
        Milestone(milestone, project, 1, "M32.19", "restart", state, NOW, NOW)
    )
    return connection, project, milestone


@pytest.mark.parametrize(
    "state",
    [
        MilestoneState.CODING,
        MilestoneState.COMMITTING,
        MilestoneState.PUSHING,
        MilestoneState.PR_CREATING,
        MilestoneState.CI_RUNNING,
        MilestoneState.ARCHITECT_REVIEW,
        MilestoneState.MERGE_READY,
    ],
)
def test_clean_restart_boundaries_remain_executor_owned(
    tmp_path: Path, state: MilestoneState
) -> None:
    """A fresh composition remains drained and performs no route mutation."""
    connection, _project, _milestone = _seed(tmp_path / f"{state.value}.db", state)
    executions = 0

    def forbidden(_job):
        nonlocal executions
        executions += 1
        raise AssertionError("scheduler dispatched while recovery was draining")

    scheduler = _scheduler(connection, {"QUEUED_ROUTE": forbidden})
    coordinator = RecoveryCoordinator(connection, scheduler, clock=lambda: NOW)
    run_id = coordinator.recover(correlation_id=f"restart-{state.value}")

    assert executions == 0
    assert coordinator.lifecycle.value == "READY"
    assert not scheduler.is_draining
    assert (
        connection.execute(
            "SELECT status FROM recovery_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        == "READY"
    )
    observation = connection.execute(
        """SELECT milestone_id,correlation_id,disposition
           FROM recovery_observations WHERE recovery_run_id=?""",
        (run_id,),
    ).fetchone()
    assert observation[0] is not None
    assert observation[1] == f"restart-{state.value}"
    assert observation[2] in {"NO_ACTION", "RESTORED_WAIT"}
    assert connection.execute("SELECT state FROM milestones").fetchone()[0] == state
    scheduler.close()
    connection.close()


@pytest.mark.parametrize(
    ("job_type", "worker", "payload"),
    [
        ("ARCHITECT_TASK", WorkerClass.ARCHITECT, {"task_type": "IMPLEMENT"}),
        ("CODEX_RUN", WorkerClass.CODEX, {}),
        ("WORKSPACE_PREPARE", WorkerClass.GIT, {}),
        ("CHANGE_VALIDATE", WorkerClass.GIT, {}),
        ("GIT_COMMIT", WorkerClass.GIT, {}),
        ("GIT_PUSH", WorkerClass.GIT, {}),
        ("PR_CREATE", WorkerClass.GITHUB, {}),
        ("CI_RECONCILE", WorkerClass.CI, {}),
        ("ARCHITECT_REVIEW", WorkerClass.ARCHITECT, {}),
    ],
)
def test_lost_workers_are_terminalized_and_replayed_with_exact_payload(
    tmp_path: Path,
    job_type: str,
    worker: WorkerClass,
    payload: dict[str, str],
) -> None:
    connection, project, milestone = _seed(
        tmp_path / f"{job_type}.db", MilestoneState.CODING
    )
    jobs = SQLiteJobRepository(connection, lambda: str(uuid4()))
    original = Job(
        JobId.generate(),
        project,
        job_type,
        JobState.QUEUED,
        0,
        NOW,
        NOW,
        milestone,
        worker_class=worker,
        payload=payload,
    )
    jobs.add(original)
    jobs.apply_transition(
        JobTransitionRequest(
            original.id,
            project,
            JobState.QUEUED,
            JobState.DISPATCHED,
            "dispatch",
            "SYSTEM",
            "acceptance",
            "lost-worker",
            NOW,
        )
    )
    jobs.apply_transition(
        JobTransitionRequest(
            original.id,
            project,
            JobState.DISPATCHED,
            JobState.RUNNING,
            "run",
            "SYSTEM",
            "acceptance",
            "lost-worker",
            NOW + timedelta(seconds=1),
        )
    )
    scheduler = _scheduler(connection)
    run_id = RecoveryCoordinator(
        connection,
        scheduler,
        workspace_clean=lambda _subject: True,
        clock=lambda: NOW + timedelta(seconds=2),
    ).recover(correlation_id=f"recover-{job_type}")

    assert jobs.get(original.id, project).state is JobState.ABANDONED
    replacement = connection.execute(
        "SELECT id,payload_json,state FROM jobs WHERE id<>?", (str(original.id),)
    ).fetchone()
    assert replacement is not None and replacement["state"] == "QUEUED"
    assert jobs.get(JobId.from_string(replacement["id"]), project).payload == payload
    observation = connection.execute(
        """SELECT job_id,disposition,observed_state_json FROM recovery_observations
           WHERE recovery_run_id=? AND job_id=?""",
        (run_id, str(original.id)),
    ).fetchone()
    assert observation is not None and observation["disposition"] == "ABANDONED"
    assert str(original.id) not in replacement["payload_json"]
    scheduler.close()
    connection.close()


def test_dirty_lost_local_work_remains_fail_closed(tmp_path: Path) -> None:
    connection, project, milestone = _seed(tmp_path / "dirty.db", MilestoneState.CODING)
    jobs = SQLiteJobRepository(connection, lambda: str(uuid4()))
    job = Job(
        JobId.generate(),
        project,
        "CODEX_RUN",
        JobState.QUEUED,
        0,
        NOW,
        NOW,
        milestone,
        worker_class=WorkerClass.CODEX,
        payload={},
    )
    jobs.add(job)
    jobs.apply_transition(
        JobTransitionRequest(
            job.id,
            project,
            JobState.QUEUED,
            JobState.DISPATCHED,
            "dispatch",
            "SYSTEM",
            "acceptance",
            "dirty",
            NOW,
        )
    )
    jobs.apply_transition(
        JobTransitionRequest(
            job.id,
            project,
            JobState.DISPATCHED,
            JobState.RUNNING,
            "run",
            "SYSTEM",
            "acceptance",
            "dirty",
            NOW + timedelta(seconds=1),
        )
    )
    scheduler = _scheduler(connection)
    RecoveryCoordinator(
        connection,
        scheduler,
        workspace_clean=lambda _subject: False,
        clock=lambda: NOW + timedelta(seconds=2),
    ).recover(correlation_id="dirty-local")
    assert jobs.get(job.id, project).state is JobState.ABANDONED
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert connection.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    assert connection.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
    scheduler.close()
    connection.close()


class _CountingArchitect(FakeArchitect):
    def __init__(self) -> None:
        self.task_calls = 0
        self.review_calls = 0

    def task(self, request):
        self.task_calls += 1
        return super().task(request)

    def review(self, request):
        self.review_calls += 1
        return super().review(request)


class _CountingActions(FakeActions):
    def __init__(self) -> None:
        self.observations = 0

    def observe(self, repository_full_name, pull_request_number, head_sha):
        self.observations += 1
        return super().observe(repository_full_name, pull_request_number, head_sha)


class _CountingGit(LocalRemoteTrustedGit):
    def __init__(self, authorised_root: Path, remote: Path) -> None:
        super().__init__(authorised_root, remote)
        self.pushes = 0

    def push(self, repository: Path, remote_url: str, branch: str) -> None:
        self.pushes += 1
        super().push(repository, remote_url, branch)


@pytest.mark.parametrize(
    ("job_type", "owner_state", "handoff_state", "next_state"),
    [
        (
            "ARCHITECT_TASK",
            "PREPARING_TASK",
            "PREPARING_WORKSPACE",
            "CODING",
        ),
        ("CODEX_RUN", "CODING", "VALIDATING_CHANGES", "COMMITTING"),
        (
            "CHANGE_VALIDATE",
            "VALIDATING_CHANGES",
            "COMMITTING",
            "PUSHING",
        ),
        ("GIT_COMMIT", "COMMITTING", "PUSHING", "PR_CREATING"),
    ],
)
def test_completed_unharvested_route_is_reconciled_before_workspace_safety(
    tmp_path: Path,
    job_type: str,
    owner_state: str,
    handoff_state: str,
    next_state: str,
) -> None:
    """Model death after executor commit but before Scheduler Future harvest."""
    database, data, seeded_git = seed_happy_path(tmp_path)
    seeded_git = cast(LocalRemoteTrustedGit, seeded_git)
    trusted_git = _CountingGit(data / "auth", Path(seeded_git.remote))
    application_config = happy_config(tmp_path, database, data)
    github, architect, actions = FakeGitHub(), _CountingArchitect(), _CountingActions()
    codex_executions = 0

    def runner(connection: sqlite3.Connection) -> WorkspaceBoundCodexRunner:
        class CountingCodex(FakeCodexProcess):
            def run(self, request, *, require_clean=True):
                nonlocal codex_executions
                codex_executions += 1
                return super().run(request)

        return cast(
            WorkspaceBoundCodexRunner, CountingCodex(connection, trusted_git, data)
        )

    def compose() -> tuple[sqlite3.Connection, Scheduler]:
        connection = open_database(database)
        executors = build_production_executors(
            application_config,
            dependencies=ProductionDependencies(
                architect_provider_factory=lambda: cast(ArchitectProvider, architect),
                codex_runner_factory=runner,
                trusted_git=trusted_git,
                github_pull_requests=github,
                github_actions_factory=lambda: actions,
            ),
        )
        due = ProductionDueWorkCoordinator(connection, clock=lambda: NOW)
        return connection, Scheduler(
            SQLiteJobRepository(connection, lambda: str(uuid4())),
            WorkerCapacity({worker: 1 for worker in WorkerClass}),
            cast(dict[WorkerClass, JobExecutor], executors),
            clock=lambda: NOW,
            due_work_enqueuer=due.enqueue_due,
        )

    def milestone_state(connection: sqlite3.Connection) -> str:
        return cast(
            str,
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(HAPPY_MILESTONE),)
            ).fetchone()[0],
        )

    def drive_harvested(
        connection: sqlite3.Connection, scheduler: Scheduler, target: str
    ) -> None:
        for _ in range(100):
            if milestone_state(connection) == target:
                scheduler.enter_drain()
                scheduler.drain_until_idle(5, poll_interval_seconds=0.01)
                scheduler.exit_drain()
                return
            scheduler.run_once()
            if scheduler.active_execution_count:
                scheduler.wait_for_wake(5)
        raise AssertionError(
            f"workflow did not reach {target}: {diagnostic(connection)}"
        )

    control, scheduler = compose()
    for _ in range(100):
        scheduler.run_once()
        running = control.execute(
            "SELECT state FROM jobs WHERE job_type=? ORDER BY rowid DESC LIMIT 1",
            (job_type,),
        ).fetchone()
        if running is not None and running["state"] == "RUNNING":
            assert scheduler.active_execution_count == 1
            scheduler.wait_for_wake(5)  # deliberately do not harvest the Future
            break
        if scheduler.active_execution_count:
            scheduler.wait_for_wake(5)
    else:
        raise AssertionError(f"route was not dispatched: {diagnostic(control)}")

    with open_database(database) as observation:
        running = observation.execute(
            "SELECT id,state FROM jobs WHERE job_type=? ORDER BY rowid DESC LIMIT 1",
            (job_type,),
        ).fetchone()
        assert running is not None and running["state"] == "RUNNING"
        assert milestone_state(observation) == handoff_state
        assert (
            observation.execute(
                """SELECT count(*) FROM state_transitions
               WHERE entity_type='MILESTONE' AND entity_id=?
                 AND previous_state=? AND new_state=?""",
                (str(HAPPY_MILESTONE), owner_state, handoff_state),
            ).fetchone()[0]
            == 1
        )
        if job_type == "ARCHITECT_TASK":
            assert (
                observation.execute(
                    """SELECT count(*) FROM architect_requests
                   WHERE job_id=? AND request_type='TASK' AND status='SUCCEEDED'""",
                    (running["id"],),
                ).fetchone()[0]
                == 1
            )
            assert (
                observation.execute("SELECT count(*) FROM git_workspaces").fetchone()[0]
                == 0
            )
        elif job_type == "CODEX_RUN":
            assert (
                observation.execute(
                    "SELECT process_status FROM codex_runs WHERE job_id=?",
                    (running["id"],),
                ).fetchone()[0]
                == "SUCCEEDED"
            )
        elif job_type == "CHANGE_VALIDATE":
            assert (
                observation.execute(
                    "SELECT count(*) FROM change_sets WHERE decision='ACCEPT'"
                ).fetchone()[0]
                == 1
            )
        else:
            assert (
                observation.execute("SELECT count(*) FROM commits").fetchone()[0] == 1
            )

    provider_counts = (architect.task_calls, codex_executions)
    scheduler.close(wait=False)
    control.close()
    control, scheduler = compose()
    workspace = WorkspaceService(control, trusted_git, data)
    RecoveryCoordinator(
        control,
        scheduler,
        services=RecoveryServices(
            gatekeeper=Gatekeeper(control, github), workspace=workspace
        ),
        workspace_clean=lambda subject: (
            subject.milestone_id is not None
            and workspace.inspect(subject.project_id, subject.milestone_id).clean
        ),
        clock=lambda: NOW + timedelta(minutes=1),
    ).recover(correlation_id=f"unharvested-{job_type}")

    assert (architect.task_calls, codex_executions) == provider_counts
    assert control.execute("SELECT state FROM projects").fetchone()[0] != "BLOCKED"
    assert milestone_state(control) == handoff_state
    rows = control.execute(
        "SELECT state FROM jobs WHERE job_type=? ORDER BY rowid", (job_type,)
    ).fetchall()
    assert [row["state"] for row in rows] == ["ABANDONED"]
    if job_type in {"CODEX_RUN", "CHANGE_VALIDATE"}:
        assert not workspace.inspect(HAPPY_PROJECT, HAPPY_MILESTONE).clean
    if job_type == "CHANGE_VALIDATE":
        assert (
            control.execute(
                "SELECT count(*) FROM change_sets WHERE decision='ACCEPT'"
            ).fetchone()[0]
            == 1
        )
    if job_type == "GIT_COMMIT":
        assert control.execute("SELECT count(*) FROM commits").fetchone()[0] == 1

    drive_harvested(control, scheduler, next_state)
    assert (
        control.execute(
            "SELECT count(*) FROM jobs WHERE job_type=?", (job_type,)
        ).fetchone()[0]
        == 1
    )
    scheduler.close()
    control.close()


def test_changes_required_review_handoff_is_reconciled_before_harvest(
    tmp_path: Path,
) -> None:
    database, data, trusted_git = seed_rework_path(tmp_path)
    trusted_git = cast(ReworkTrustedGit, trusted_git)
    codex_requests: list[CodexRunRequest] = []

    def remote_head() -> str:
        heads = rework_git(
            Path(trusted_git.remote),
            "for-each-ref",
            "--format=%(objectname)",
            "refs/heads/syntra/*",
        ).splitlines()
        assert len(heads) == 1
        return heads[0]

    github, architect, actions = (
        ReworkGitHub(remote_head),
        ReworkArchitect(),
        ReworkActions(),
    )
    application_config = rework_config(tmp_path, database, data)

    def runner(connection: sqlite3.Connection) -> WorkspaceBoundCodexRunner:
        return cast(
            WorkspaceBoundCodexRunner,
            ReworkCodexProcess(connection, trusted_git, data, codex_requests),
        )

    def compose() -> tuple[sqlite3.Connection, Scheduler]:
        connection = open_database(database)
        executors = build_production_executors(
            application_config,
            dependencies=ProductionDependencies(
                architect_provider_factory=lambda: cast(ArchitectProvider, architect),
                codex_runner_factory=runner,
                trusted_git=trusted_git,
                github_pull_requests=github,
                github_actions_factory=lambda: actions,
            ),
        )
        due = ProductionDueWorkCoordinator(connection, clock=lambda: datetime.now(UTC))
        return connection, Scheduler(
            SQLiteJobRepository(connection, lambda: str(uuid4())),
            WorkerCapacity({worker: 1 for worker in WorkerClass}),
            cast(dict[WorkerClass, JobExecutor], executors),
            clock=lambda: datetime.now(UTC),
            due_work_enqueuer=due.enqueue_due,
        )

    control, scheduler = compose()
    for _ in range(150):
        scheduler.run_once()
        review_job = control.execute(
            """SELECT id,state FROM jobs WHERE job_type='ARCHITECT_REVIEW'
               ORDER BY rowid DESC LIMIT 1"""
        ).fetchone()
        if review_job is not None and review_job["state"] == "RUNNING":
            scheduler.wait_for_wake(5)
            with open_database(database) as observation:
                if (
                    observation.execute(
                        "SELECT state FROM milestones WHERE id=?",
                        (str(REWORK_MILESTONE),),
                    ).fetchone()[0]
                    == "CODING"
                ):
                    break
        elif scheduler.active_execution_count:
            scheduler.wait_for_wake(5)
    else:
        raise AssertionError("CHANGES_REQUIRED review did not reach its handoff")

    with open_database(database) as observation:
        assert (
            observation.execute(
                "SELECT state FROM jobs WHERE id=?", (review_job["id"],)
            ).fetchone()[0]
            == "RUNNING"
        )
        assert (
            observation.execute("SELECT verdict FROM architect_reviews").fetchone()[0]
            == "CHANGES_REQUIRED"
        )
        assert (
            observation.execute(
                "SELECT state FROM milestones WHERE id=?", (str(REWORK_MILESTONE),)
            ).fetchone()[0]
            == "CODING"
        )
        assert (
            observation.execute(
                "SELECT count(*) FROM architect_rework_tasks"
            ).fetchone()[0]
            == 1
        )
        assert (
            observation.execute(
                "SELECT count(*) FROM jobs WHERE job_type='CODEX_REVIEW_REWORK'"
            ).fetchone()[0]
            == 1
        )

    scheduler.close(wait=False)
    control.close()

    control, scheduler = compose()
    workspace = WorkspaceService(control, trusted_git, data)
    RecoveryCoordinator(
        control,
        scheduler,
        services=RecoveryServices(
            gatekeeper=Gatekeeper(control, github), workspace=workspace
        ),
        workspace_clean=lambda subject: (
            subject.milestone_id is not None
            and workspace.inspect(subject.project_id, subject.milestone_id).clean
        ),
    ).recover(correlation_id="unharvested-changes-required-review")

    assert len(architect.requests) == 1
    assert (
        control.execute(
            "SELECT state FROM jobs WHERE id=?", (review_job["id"],)
        ).fetchone()[0]
        == "ABANDONED"
    )
    assert (
        control.execute(
            "SELECT count(*) FROM jobs WHERE job_type='ARCHITECT_REVIEW'"
        ).fetchone()[0]
        == 1
    )
    assert (
        control.execute(
            "SELECT count(*) FROM jobs WHERE job_type='CODEX_REVIEW_REWORK'"
        ).fetchone()[0]
        == 1
    )
    assert (
        control.execute(
            "SELECT state FROM milestones WHERE id=?", (str(REWORK_MILESTONE),)
        ).fetchone()[0]
        == "CODING"
    )
    assert (
        control.execute(
            "SELECT state FROM projects WHERE id=?", (str(REWORK_PROJECT),)
        ).fetchone()[0]
        == "BUILDING"
    )

    for _ in range(10):
        scheduler.run_once()
        if len(codex_requests) == 2:
            break
        scheduler.wait_for_wake(5)
    else:
        raise AssertionError("existing review-rework Codex job did not execute")
    assert [request.task["task_type"] for request in codex_requests] == [
        "IMPLEMENT",
        "REVIEW_REWORK",
    ]
    assert (
        control.execute(
            "SELECT count(*) FROM jobs WHERE job_type='CODEX_RUN'"
        ).fetchone()[0]
        == 1
    )
    scheduler.close(wait=False)
    control.close()


def test_superseded_review_is_reconciled_without_provider_replay(
    tmp_path: Path,
) -> None:
    seeded_connection, project, milestone, _pr, descriptor = seed_review(tmp_path)
    database = Path(seeded_connection.execute("PRAGMA database_list").fetchone()[2])
    seeded_connection.close()
    github = StaleGitHub(descriptor)
    github.after_call = replace(descriptor, head_sha=SHA_B)
    architect = StaleArchitect(ArchitectReviewVerdict.APPROVE)
    executor = ArchitectReviewExecutor(
        database,
        lambda connection: ArchitectReviewService(
            connection, github, github, architect, clock=lambda: NOW
        ),
        clock=lambda: NOW,
    )
    control = open_database(database)
    jobs = SQLiteJobRepository(control, lambda: str(uuid4()))
    review_job = Job(
        JobId.generate(),
        project,
        "ARCHITECT_REVIEW",
        JobState.QUEUED,
        0,
        NOW,
        NOW,
        milestone,
        correlation_id="stale-unharvested-review",
        worker_class=WorkerClass.ARCHITECT,
        payload={},
    )
    jobs.add(review_job)
    scheduler = Scheduler(
        jobs,
        WorkerCapacity({worker: 1 for worker in WorkerClass}),
        {WorkerClass.ARCHITECT: executor},
        clock=lambda: NOW,
    )
    scheduler.run_once()
    scheduler.wait_for_wake(5)

    with open_database(database) as observation:
        assert jobs.get(review_job.id, project).state is JobState.RUNNING
        review = observation.execute(
            "SELECT verdict,superseded_at FROM architect_reviews"
        ).fetchone()
        assert review["verdict"] == "APPROVE"
        assert review["superseded_at"] is not None
        assert (
            observation.execute(
                "SELECT state FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()[0]
            == "BLOCKED"
        )

    scheduler.close(wait=False)
    control.close()
    control = open_database(database)
    jobs = SQLiteJobRepository(control, lambda: str(uuid4()))
    scheduler = _scheduler(control)
    RecoveryCoordinator(
        control,
        scheduler,
        workspace_clean=lambda _subject: False,
        clock=lambda: NOW + timedelta(seconds=1),
    ).recover(correlation_id="recover-stale-unharvested-review")

    assert len(architect.requests) == 1
    assert jobs.get(review_job.id, project).state is JobState.ABANDONED
    assert (
        control.execute(
            "SELECT count(*) FROM jobs WHERE job_type='ARCHITECT_REVIEW'"
        ).fetchone()[0]
        == 1
    )
    assert (
        control.execute(
            "SELECT state FROM milestones WHERE id=?", (str(milestone),)
        ).fetchone()[0]
        == "BLOCKED"
    )
    assert (
        control.execute("SELECT superseded_at FROM architect_reviews").fetchone()[0]
        is not None
    )
    scheduler.close()
    control.close()


def test_repeated_real_restarts_complete_one_production_composed_milestone(
    tmp_path: Path,
) -> None:
    """Restart fresh production composition at every meaningful M32 boundary."""
    database, data, seeded_git = seed_happy_path(tmp_path)
    seeded_git = cast(LocalRemoteTrustedGit, seeded_git)
    trusted_git = _CountingGit(data / "auth", Path(seeded_git.remote))
    application_config = happy_config(tmp_path, database, data)
    github = FakeGitHub()
    architect = _CountingArchitect()
    actions = _CountingActions()
    codex_executions = 0
    recovery_runs: list[str] = []

    def runner(connection: sqlite3.Connection) -> WorkspaceBoundCodexRunner:
        class CountingCodex(FakeCodexProcess):
            def run(self, request, *, require_clean=True):
                nonlocal codex_executions
                codex_executions += 1
                return super().run(request)

        return cast(
            WorkspaceBoundCodexRunner, CountingCodex(connection, trusted_git, data)
        )

    def compose() -> tuple[sqlite3.Connection, Scheduler]:
        control = open_database(database)
        dependencies = ProductionDependencies(
            architect_provider_factory=lambda: cast(ArchitectProvider, architect),
            codex_runner_factory=runner,
            trusted_git=trusted_git,
            github_pull_requests=github,
            github_actions_factory=lambda: actions,
        )
        executors = build_production_executors(
            application_config, dependencies=dependencies
        )
        due = ProductionDueWorkCoordinator(control, clock=lambda: NOW)
        scheduler = Scheduler(
            SQLiteJobRepository(control, lambda: str(uuid4())),
            WorkerCapacity({worker: 1 for worker in WorkerClass}),
            cast(dict[WorkerClass, JobExecutor], executors),
            clock=lambda: NOW,
            due_work_enqueuer=due.enqueue_due,
        )
        return control, scheduler

    def state(connection) -> str:
        return cast(
            str,
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(HAPPY_MILESTONE),)
            ).fetchone()[0],
        )

    def drive_to(connection, scheduler, target: str) -> None:
        for _ in range(100):
            if state(connection) == target:
                scheduler.enter_drain()
                scheduler.drain_until_idle(5, poll_interval_seconds=0.01)
                assert scheduler.active_execution_count == 0
                assert state(connection) == target
                return
            scheduler.run_once()
            if scheduler.active_execution_count:
                scheduler.wait_for_wake(5)
        raise AssertionError(
            f"workflow did not reach {target}: {diagnostic(connection)}"
        )

    def side_effects() -> tuple[int, ...]:
        return (
            codex_executions,
            trusted_git.pushes,
            github.create_calls,
            actions.observations,
            architect.review_calls,
            github.merge_calls,
        )

    control, scheduler = compose()
    checkpoints = (
        "CODING",
        "COMMITTING",
        "PUSHING",
        "PR_CREATING",
        "CI_RUNNING",
        "ARCHITECT_REVIEW",
        "MERGE_READY",
    )
    for index, checkpoint in enumerate(checkpoints, start=1):
        drive_to(control, scheduler, checkpoint)
        if checkpoint == "COMMITTING":
            assert (
                control.execute("SELECT decision FROM change_sets").fetchone()[0]
                == "ACCEPT"
            )
            assert control.execute("SELECT count(*) FROM commits").fetchone()[0] == 0
        elif checkpoint == "PUSHING":
            assert control.execute("SELECT count(*) FROM commits").fetchone()[0] == 1
            assert trusted_git.pushes == 0
        elif checkpoint == "PR_CREATING":
            assert trusted_git.pushes == 1
            assert github.create_calls == 0
        elif checkpoint == "CI_RUNNING":
            assert github.create_calls == 1
            assert actions.observations == 0
        elif checkpoint == "ARCHITECT_REVIEW":
            assert actions.observations == 1
            assert architect.review_calls == 0
        elif checkpoint == "MERGE_READY":
            assert architect.review_calls == 1
            assert (
                control.execute("SELECT count(*) FROM merge_attempts").fetchone()[0]
                == 0
            )
            assert github.merge_calls == 0

        scheduler.close()
        control.close()
        control, scheduler = compose()
        before = side_effects()
        workspace = WorkspaceService(control, trusted_git, data)
        coordinator = RecoveryCoordinator(
            control,
            scheduler,
            services=RecoveryServices(
                gatekeeper=Gatekeeper(control, github), workspace=workspace
            ),
            workspace_clean=lambda subject: (
                subject.milestone_id is not None
                and workspace.inspect(subject.project_id, subject.milestone_id).clean
            ),
            clock=lambda: NOW + timedelta(seconds=index),
        )
        run_id = coordinator.recover(correlation_id=f"m32.19-restart-{checkpoint}")
        assert side_effects() == before
        assert coordinator.lifecycle.value == "READY"
        assert not scheduler.is_draining
        recovery_runs.append(run_id)

    for _ in range(100):
        if state(control) == "COMPLETE":
            scheduler.enter_drain()
            scheduler.drain_until_idle(5, poll_interval_seconds=0.01)
            break
        scheduler.run_once()
        if scheduler.active_execution_count:
            scheduler.wait_for_wake(5)
    else:
        raise AssertionError(f"workflow did not complete: {diagnostic(control)}")

    jobs = control.execute("SELECT job_type,state FROM jobs ORDER BY rowid").fetchall()
    assert [row["job_type"] for row in jobs] == EXPECTED_JOBS
    assert {row["state"] for row in jobs} == {"SUCCEEDED"}
    assert side_effects() == (1, 1, 1, 1, 1, 1)
    assert architect.task_calls == 1
    assert control.execute("SELECT count(*) FROM commits").fetchone()[0] == 1
    assert (
        control.execute(
            "SELECT count(*) FROM commits WHERE pushed_at IS NOT NULL"
        ).fetchone()[0]
        == 1
    )
    assert (
        control.execute(
            "SELECT count(*) FROM ci_runs WHERE overall_status='PASSED'"
        ).fetchone()[0]
        == 1
    )
    assert (
        control.execute(
            "SELECT count(*) FROM architect_reviews WHERE verdict='APPROVE'"
        ).fetchone()[0]
        == 1
    )
    assert control.execute("SELECT count(*) FROM merge_attempts").fetchone()[0] == 1
    assert len(recovery_runs) == len(checkpoints)
    runs = control.execute(
        "SELECT id,status,correlation_id FROM recovery_runs ORDER BY rowid"
    ).fetchall()
    assert [row["id"] for row in runs] == recovery_runs
    assert all(row["status"] == "READY" and row["correlation_id"] for row in runs)
    assert all(
        control.execute(
            "SELECT count(*) FROM recovery_observations WHERE recovery_run_id=?",
            (run_id,),
        ).fetchone()[0]
        > 0
        for run_id in recovery_runs
    )
    scheduler.close()
    control.close()


def test_crash_after_merge_mutation_is_observed_without_second_put(
    tmp_path: Path,
) -> None:
    """A vanished PR_MERGE worker is terminalized only after exact observation."""
    connection = open_database(tmp_path / "merge-crash.db")
    apply_migrations(connection)
    seed, github = seed_eligible(connection)
    gatekeeper = Gatekeeper(connection, github)
    attempt_id, _request = gatekeeper.prepare(
        seed.request, MergeStrategy.SQUASH, now=MERGE_NOW
    )
    connection.execute(
        "UPDATE merge_attempts SET mutation_started_at=? WHERE id=?",
        (MERGE_NOW.isoformat(), attempt_id),
    )
    github.live = replace(
        github.live,
        state=PullRequestState.MERGED,
        merged_at=MERGE_NOW.isoformat(),
        merge_commit_sha=MERGE_SHA,
    )
    jobs = SQLiteJobRepository(connection, lambda: str(uuid4()))
    orphan = Job(
        JobId.generate(),
        seed.project,
        "PR_MERGE",
        JobState.QUEUED,
        0,
        MERGE_NOW,
        MERGE_NOW,
        seed.milestone,
        worker_class=WorkerClass.GITHUB,
        payload={},
    )
    jobs.add(orphan)
    jobs.apply_transition(
        JobTransitionRequest(
            orphan.id,
            seed.project,
            JobState.QUEUED,
            JobState.DISPATCHED,
            "dispatch",
            "SYSTEM",
            "acceptance",
            "merge-crash",
            MERGE_NOW,
        )
    )
    jobs.apply_transition(
        JobTransitionRequest(
            orphan.id,
            seed.project,
            JobState.DISPATCHED,
            JobState.RUNNING,
            "run",
            "SYSTEM",
            "acceptance",
            "merge-crash",
            MERGE_NOW + timedelta(seconds=1),
        )
    )
    github.get_calls.clear()
    scheduler = _scheduler(connection)
    run_id = RecoveryCoordinator(
        connection,
        scheduler,
        services=RecoveryServices(gatekeeper=gatekeeper),
        clock=lambda: MERGE_NOW + timedelta(seconds=2),
    ).recover(correlation_id="merge-mutation-crash")

    assert github.merge_calls == []
    assert len(github.get_calls) == 2
    assert (
        connection.execute(
            "SELECT status FROM merge_attempts WHERE id=?", (attempt_id,)
        ).fetchone()[0]
        == "MERGED"
    )
    assert (
        connection.execute("SELECT state FROM pull_requests").fetchone()[0] == "MERGED"
    )
    assert (
        connection.execute("SELECT state FROM milestones").fetchone()[0] == "COMPLETE"
    )
    assert jobs.get(orphan.id, seed.project).state is JobState.ABANDONED
    assert (
        connection.execute(
            """SELECT count(*) FROM recovery_observations
           WHERE recovery_run_id=? AND disposition='RECONCILED'""",
            (run_id,),
        ).fetchone()[0]
        >= 1
    )
    scheduler.close()
    connection.close()


def test_ambiguous_merge_abandons_orphan_and_never_replays_put(
    tmp_path: Path,
) -> None:
    connection = open_database(tmp_path / "ambiguous-merge.db")
    apply_migrations(connection)
    seed, github = seed_eligible(connection)
    gatekeeper = Gatekeeper(connection, github)
    attempt_id, _request = gatekeeper.prepare(
        seed.request, MergeStrategy.SQUASH, now=MERGE_NOW
    )
    connection.execute(
        "UPDATE merge_attempts SET mutation_started_at=? WHERE id=?",
        (MERGE_NOW.isoformat(), attempt_id),
    )
    jobs = SQLiteJobRepository(connection, lambda: str(uuid4()))
    orphan = Job(
        JobId.generate(),
        seed.project,
        "PR_MERGE",
        JobState.QUEUED,
        0,
        MERGE_NOW,
        MERGE_NOW,
        seed.milestone,
        worker_class=WorkerClass.GITHUB,
        payload={},
    )
    jobs.add(orphan)
    jobs.apply_transition(
        JobTransitionRequest(
            orphan.id,
            seed.project,
            JobState.QUEUED,
            JobState.DISPATCHED,
            "dispatch",
            "SYSTEM",
            "acceptance",
            "ambiguous-merge",
            MERGE_NOW,
        )
    )
    jobs.apply_transition(
        JobTransitionRequest(
            orphan.id,
            seed.project,
            JobState.DISPATCHED,
            JobState.RUNNING,
            "run",
            "SYSTEM",
            "acceptance",
            "ambiguous-merge",
            MERGE_NOW + timedelta(seconds=1),
        )
    )
    scheduler = _scheduler(connection)
    RecoveryCoordinator(
        connection,
        scheduler,
        services=RecoveryServices(gatekeeper=gatekeeper),
        clock=lambda: MERGE_NOW + timedelta(seconds=2),
    ).recover(correlation_id="ambiguous-merge-first")

    assert github.merge_calls == []
    assert connection.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    assert connection.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
    assert jobs.get(orphan.id, seed.project).state is JobState.ABANDONED
    assert (
        connection.execute(
            "SELECT count(*) FROM jobs WHERE job_type='PR_MERGE'"
        ).fetchone()[0]
        == 1
    )
    attempt = connection.execute(
        "SELECT status,mutation_started_at FROM merge_attempts WHERE id=?",
        (attempt_id,),
    ).fetchone()
    assert attempt["status"] != "MERGED"
    assert attempt["mutation_started_at"] is not None

    RecoveryCoordinator(
        connection,
        scheduler,
        services=RecoveryServices(gatekeeper=gatekeeper),
        clock=lambda: MERGE_NOW + timedelta(seconds=3),
    ).recover(correlation_id="ambiguous-merge-repeat")
    assert github.merge_calls == []
    assert jobs.get(orphan.id, seed.project).state is JobState.ABANDONED
    assert (
        connection.execute(
            "SELECT count(*) FROM jobs WHERE job_type='PR_MERGE'"
        ).fetchone()[0]
        == 1
    )
    scheduler.close()
    connection.close()
