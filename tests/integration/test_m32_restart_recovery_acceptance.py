# mypy: disable-error-code="no-untyped-def"
"""M32.19 acceptance: restart recovery restores, but does not execute, M32 routes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_m26_gatekeeper import MERGE_SHA, seed_eligible
from test_m26_gatekeeper import NOW as MERGE_NOW

from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.application.recovery import RecoveryCoordinator, RecoveryServices
from syntra_build.application.scheduler import Scheduler, WorkerCapacity
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
from syntra_build.domain.merges import MergeStrategy
from syntra_build.domain.pull_requests import PullRequestState
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
        ("ARCHITECT_TASK", WorkerClass.ARCHITECT, {"task_type": "design"}),
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
