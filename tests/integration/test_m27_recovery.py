from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_m26_gatekeeper import NOW as M26_NOW
from test_m26_gatekeeper import seed_eligible

from syntra_build.application.gatekeeper import Gatekeeper
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
from syntra_build.domain.merges import MergeStrategy
from syntra_build.domain.recovery import (
    RecoveryDecision,
    RecoveryDisposition,
    RecoverySubject,
)
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
    assert current_schema_version(db) == 28
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


def test_024_to_025_preserves_m25_m26_and_active_attempt_history(
    tmp_path: Path,
) -> None:
    db = open_database(tmp_path / "representative-upgrade.db")
    apply_migrations(db, MIGRATIONS[:24])
    seed, github = seed_eligible(db)
    attempt_id, _ = Gatekeeper(db, github).prepare(
        seed.request, MergeStrategy.SQUASH, now=M26_NOW
    )
    jobs = SQLiteJobRepository(db, lambda: str(uuid4()))
    job_id = JobId.generate()
    jobs.add(
        Job(
            job_id,
            seed.project,
            "RECOVERY_FIXTURE",
            JobState.QUEUED,
            0,
            M26_NOW,
            M26_NOW,
            seed.milestone,
            worker_class=WorkerClass.CODEX,
            max_attempts=2,
        )
    )
    jobs.apply_transition(
        JobTransitionRequest(
            job_id,
            seed.project,
            JobState.QUEUED,
            JobState.DISPATCHED,
            "dispatch",
            "SYSTEM",
            "fixture",
            "upgrade",
            M26_NOW,
        )
    )
    jobs.apply_transition(
        JobTransitionRequest(
            job_id,
            seed.project,
            JobState.DISPATCHED,
            JobState.RUNNING,
            "run",
            "SYSTEM",
            "fixture",
            "upgrade",
            M26_NOW + timedelta(seconds=1),
        )
    )
    gate_id, response_id = str(uuid4()), str(uuid4())
    db.execute(
        """INSERT INTO human_gates
           (id,project_id,milestone_id,gate_type,state,title,prompt,
            expected_response_type,options_json,resume_milestone_state,
            created_at,notified_at,responded_at,resolved_at,created_by,
            correlation_id,architect_review_id,causation_id)
           VALUES (?,?,?,'HUMAN_TEST','RESOLVED','test','test','HUMAN_TEST','[]',
            'ARCHITECT_REVIEW',?,?,?,?,?,'fixture',?,'review-fixture')""",
        (
            gate_id,
            str(seed.project),
            str(seed.milestone),
            M26_NOW.isoformat(),
            M26_NOW.isoformat(),
            M26_NOW.isoformat(),
            M26_NOW.isoformat(),
            "fixture",
            seed.review,
        ),
    )
    db.execute(
        """INSERT INTO human_gate_responses
           (id,gate_id,message_id,response_code,attachments_json,responded_by,
            responded_at,validated,validation_notes)
           VALUES (?,?,?,'PASS','[]','42',?,1,'valid')""",
        (response_id, gate_id, "upgrade-message", M26_NOW.isoformat()),
    )
    db.execute(
        """INSERT INTO human_test_bindings VALUES
           (?,?,?,?,?,7,?,?,NULL,'test',?)""",
        (
            gate_id,
            str(seed.project),
            str(seed.milestone),
            seed.review,
            seed.pr,
            "a" * 40,
            seed.ci,
            M26_NOW.isoformat(),
        ),
    )
    db.execute(
        "INSERT INTO human_test_results VALUES (?,?,?,'PASS',NULL,?,?,?)",
        (str(uuid4()), gate_id, response_id, "a" * 40, seed.ci, M26_NOW.isoformat()),
    )
    db.execute(
        """INSERT INTO m25_telegram_feedback_interactions
           (id,gate_id,outcome,chat_id,user_id,prompt_message_id,state,created_at)
           VALUES (?,?,'FAIL','300','42','prompt','WAITING_FEEDBACK',?)""",
        (str(uuid4()), gate_id, M26_NOW.isoformat()),
    )
    tables = (
        "projects",
        "milestones",
        "jobs",
        "job_attempts",
        "human_gates",
        "human_test_bindings",
        "human_test_results",
        "m25_telegram_feedback_interactions",
        "merge_eligibility_results",
        "merge_attempts",
    )
    before = {
        name: tuple(
            tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")
        )
        for name in tables
    }
    assert (
        db.execute(
            "SELECT name FROM sqlite_master WHERE name='recovery_observations'"
        ).fetchone()
        is None
    )
    apply_migrations(db)
    after = {
        name: tuple(
            tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")
        )
        for name in tables
    }
    # M32.14 appends the nullable mutation crash-boundary column without
    # rewriting any pre-existing attempt value.
    assert after["merge_attempts"][0][:-1] == before["merge_attempts"][0]
    assert after["merge_attempts"][0][-1] is None
    assert {k: v for k, v in after.items() if k != "merge_attempts"} == {
        k: v for k, v in before.items() if k != "merge_attempts"
    }
    assert (
        db.execute(
            "SELECT status FROM merge_attempts WHERE id=?", (attempt_id,)
        ).fetchone()[0]
        == "REQUESTED"
    )
    assert current_schema_version(db) == 28
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


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


def test_clean_lost_codex_queues_one_idempotent_replacement(tmp_path: Path) -> None:
    db, project, milestone = _seed(tmp_path / "clean-codex.db", MilestoneState.CODING)
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
            payload={"task": "preserve"},
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
        workspace_clean=lambda _: True,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    coordinator.recover(correlation_id="clean-codex")
    coordinator.recover(correlation_id="clean-codex-repeat")
    assert jobs.get(job_id, project).state is JobState.ABANDONED
    assert len(jobs.attempts(job_id, project)) == 1
    replacements = db.execute(
        """SELECT * FROM jobs WHERE id<>? AND project_id=? AND milestone_id=?
           AND job_type='CODEX_RUN'""",
        (str(job_id), str(project), str(milestone)),
    ).fetchall()
    assert len(replacements) == 1 and replacements[0]["state"] == "QUEUED"
    assert f'"replaces_job_id":"{job_id}"' in replacements[0]["payload_json"]
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "CODING"
    scheduler.close()
    db.close()


def test_operational_health_tracks_real_recovery_and_drain(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    from syntra_build.application.operational_health import OperationalHealth
    from syntra_build.domain.health import HealthState, ResourceSnapshot
    from syntra_build.infrastructure.config.models import ResourceThresholdConfig
    from syntra_build.infrastructure.health_http import HealthHTTPServer
    from syntra_build.infrastructure.metrics import MetricsService

    class Resources:
        free = 50.0

        def sample(self) -> ResourceSnapshot:
            return ResourceSnapshot(
                100, int(100 - self.free), int(self.free), self.free
            )

    database = tmp_path / "health-recovery.db"
    db, _, _ = _seed(database, MilestoneState.PR_CREATING)
    capacity = WorkerCapacity(SchedulerConfig().worker_class_limits())
    scheduler = Scheduler(SQLiteJobRepository(db, lambda: "unused"), capacity, {})
    resources = Resources()
    health = OperationalHealth(resources, ResourceThresholdConfig())
    observed: list[tuple[HealthState, bool, bool]] = []

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True)

    server = HealthHTTPServer(
        "127.0.0.1", 0, health, MetricsService(connect, health, capacity)
    )
    server.start()
    host, port = server.address

    def readiness() -> int:
        try:
            with urllib.request.urlopen(
                f"http://{host}:{port}/ready", timeout=2
            ) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)

    def inspect(subject: RecoverySubject, correlation_id: str) -> RecoveryDecision:
        del subject, correlation_id
        observed.append(
            (
                health.projection().state,
                health.projection().ready,
                scheduler.is_draining,
            )
        )
        assert readiness() == 503
        assert scheduler.run_once().dispatched == 0
        return RecoveryDecision(
            "PR_CREATING", RecoveryDisposition.BLOCKED, "controlled"
        )

    coordinator = RecoveryCoordinator(
        db,
        scheduler,
        handlers={"PR_CREATING": inspect},
        health_sink=health,
    )
    assert health.projection().state is HealthState.STARTING
    assert not health.projection().ready
    assert readiness() == 503
    coordinator.recover()
    assert observed == [(HealthState.RECOVERING, False, True)]
    assert not scheduler.is_draining
    assert health.projection().state is HealthState.HEALTHY
    assert health.projection().ready
    assert readiness() == 200

    resources.free = 7
    coordinator.recover()
    assert health.projection().state is HealthState.DEGRADED
    assert health.projection().ready
    assert readiness() == 200
    server.stop()
    scheduler.close()
    db.close()


def test_global_recovery_failure_keeps_drain_and_health_unsafe(tmp_path: Path) -> None:
    from syntra_build.application.operational_health import OperationalHealth
    from syntra_build.domain.health import HealthReason, HealthState, ResourceSnapshot
    from syntra_build.infrastructure.config.models import ResourceThresholdConfig

    class Resources:
        def sample(self) -> ResourceSnapshot:
            return ResourceSnapshot(100, 50, 50, 50)

    db, _, _ = _seed(tmp_path / "health-recovery-failure.db", MilestoneState.READY)
    scheduler = _scheduler(db)
    health = OperationalHealth(Resources(), ResourceThresholdConfig())
    coordinator = RecoveryCoordinator(db, scheduler, health_sink=health)

    def fail_discovery() -> tuple[RecoverySubject, ...]:
        raise RuntimeError("synthetic global recovery failure")

    coordinator.repository.discover = fail_discovery  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="synthetic global"):
        coordinator.recover()
    projection = health.projection()
    assert projection.state is HealthState.UNHEALTHY
    assert projection.ready is False
    assert projection.reasons == (HealthReason.RECOVERY_FAILED,)
    assert scheduler.is_draining
    scheduler.close()
    db.close()
