from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.application.recovery import RecoveryCoordinator
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
from syntra_build.domain.recovery import RecoveryDecision, RecoveryDisposition
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    SQLiteJobRepository,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    apply_migrations,
    current_schema_version,
    open_database,
)

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def _scheduler(db: sqlite3.Connection) -> Scheduler:
    limits = SchedulerConfig().worker_class_limits()
    return Scheduler(
        SQLiteJobRepository(db, lambda: "unused"), WorkerCapacity(limits), {}
    )


def _seed(
    path: Path, state: MilestoneState
) -> tuple[sqlite3.Connection, ProjectId, MilestoneId]:
    db = open_database(path)
    apply_migrations(db)
    project, milestone = ProjectId.generate(), MilestoneId.generate()
    SQLiteProjectRepository(db, lambda: "ph").add(
        Project(project, "recovery", ProjectState.BUILDING, NOW, NOW)
    )
    SQLiteMilestoneRepository(db, lambda: "mh").add(
        Milestone(milestone, project, 1, "M27", "recovery", state, NOW, NOW)
    )
    return db, project, milestone


def test_024_to_025_preserves_active_work_and_adds_append_only_audit(
    tmp_path: Path,
) -> None:
    db = open_database(tmp_path / "upgrade.db")
    apply_migrations(db, MIGRATIONS[:24])
    project = ProjectId.generate()
    SQLiteProjectRepository(db, lambda: "h").add(
        Project(project, "active", ProjectState.BUILDING, NOW, NOW)
    )
    before = tuple(db.execute("SELECT * FROM projects").fetchone())
    assert (
        db.execute(
            "SELECT name FROM sqlite_master WHERE name='recovery_runs'"
        ).fetchone()
        is None
    )
    apply_migrations(db)
    assert current_schema_version(db) == 25
    assert db.execute("SELECT state FROM projects").fetchone()[0] == "BUILDING"
    assert tuple(db.execute("SELECT * FROM projects").fetchone()) == before
    names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger')"
        )
    }
    assert {
        "recovery_runs",
        "recovery_observations",
        "recovery_observations_run",
        "recovery_observations_project",
        "recovery_observations_no_update",
        "recovery_observations_no_delete",
        "recovery_runs_no_delete",
    } <= names
    run, observation = str(uuid4()), str(uuid4())
    db.execute(
        "INSERT INTO recovery_runs VALUES (?,?,'RECOVERING',?,NULL,NULL)",
        (run, "migration", NOW.isoformat()),
    )
    db.execute(
        """INSERT INTO recovery_observations VALUES
           (?,?,?,NULL,NULL,'PROJECT','BUILDING','{}','NO_ACTION',
            'preserved','migration',NULL,?)""",
        (observation, run, str(project), NOW.isoformat()),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE recovery_observations SET resulting_action='x'")
    with pytest.raises(sqlite3.IntegrityError, match="preservation-oriented"):
        db.execute("DELETE FROM recovery_observations")
    with pytest.raises(sqlite3.IntegrityError, match="preservation-oriented"):
        db.execute("DELETE FROM recovery_runs")
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_recovery_barrier_observes_all_restart_states_without_blind_mutation(
    tmp_path: Path,
) -> None:
    for state in (
        MilestoneState.PR_CREATING,
        MilestoneState.CI_RUNNING,
        MilestoneState.MERGING,
        MilestoneState.CODING,
        MilestoneState.HUMAN_TEST,
    ):
        db, project, milestone = _seed(tmp_path / f"{state}.db", state)
        calls: list[str] = []

        def observed(subject: object, correlation_id: str) -> RecoveryDecision:
            del subject, correlation_id
            calls.append(state.value)
            return RecoveryDecision(
                state.value,
                RecoveryDisposition.RECONCILED,
                "observed; no mutation replay",
            )

        scheduler = _scheduler(db)
        coordinator = RecoveryCoordinator(
            db, scheduler, handlers={state.value: observed}, clock=lambda: NOW
        )
        run_id = coordinator.recover(correlation_id=f"recover-{state}")
        assert calls == [state.value]
        assert not scheduler.is_draining
        row = db.execute(
            "SELECT disposition FROM recovery_observations WHERE recovery_run_id=?",
            (run_id,),
        ).fetchone()
        assert row[0] == "RECONCILED"
        assert (
            SQLiteMilestoneRepository(db, lambda: "x").get(milestone, project).state
            is state
        )
        scheduler.close()
        db.close()


def test_lost_codex_is_abandoned_once_and_dirty_worktree_is_preserved(
    tmp_path: Path,
) -> None:
    db, project, milestone = _seed(tmp_path / "codex.db", MilestoneState.CODING)
    jobs = SQLiteJobRepository(db, lambda: str(uuid4()))
    job_id = JobId.generate()
    jobs.add(
        Job(
            job_id,
            project,
            "CODEX_RUN",
            JobState.QUEUED,
            0,
            NOW,
            NOW,
            milestone,
            worker_class=WorkerClass.CODEX,
            max_attempts=2,
        )
    )
    jobs.apply_transition(
        JobTransitionRequest(
            job_id,
            project,
            JobState.QUEUED,
            JobState.DISPATCHED,
            "dispatch",
            "SYSTEM",
            "test",
            "corr",
            NOW,
        )
    )
    jobs.apply_transition(
        JobTransitionRequest(
            job_id,
            project,
            JobState.DISPATCHED,
            JobState.RUNNING,
            "run",
            "SYSTEM",
            "test",
            "corr",
            NOW + timedelta(seconds=1),
        )
    )
    scheduler = _scheduler(db)
    coordinator = RecoveryCoordinator(
        db,
        scheduler,
        workspace_clean=lambda _: False,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    coordinator.recover(correlation_id="lost-codex")
    assert jobs.get(job_id, project).state is JobState.ABANDONED
    assert jobs.attempts(job_id, project)[0].state.value == "ABANDONED"
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
    scheduler.close()
    db.close()
