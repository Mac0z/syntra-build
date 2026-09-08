# ruff: noqa: E501
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    AttemptLimitExhaustedError,
    JobMilestoneProjectMismatchError,
    JobProjectMismatchError,
    PersistenceError,
    SQLiteJobRepository,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    StaleJobStateError,
    TerminalJobMutationError,
    apply_migrations,
    current_schema_version,
    open_database,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
P1 = ProjectId.from_string("00000000-0000-0000-0000-000000000001")
P2 = ProjectId.from_string("00000000-0000-0000-0000-000000000002")
M1 = MilestoneId.from_string("00000000-0000-0000-0000-000000000011")
M2 = MilestoneId.from_string("00000000-0000-0000-0000-000000000012")


def setup(
    path: Path, max_attempts: int = 2, milestone: MilestoneId | None = M1
) -> tuple[sqlite3.Connection, SQLiteJobRepository, JobId]:
    db = open_database(path)
    apply_migrations(db)
    pr = SQLiteProjectRepository(db, lambda: "p-history")
    pr.add(Project(P1, "one", ProjectState.NEW, NOW, NOW))
    pr.add(Project(P2, "two", ProjectState.NEW, NOW, NOW))
    mr = SQLiteMilestoneRepository(db, lambda: "m-history")
    mr.add(Milestone(M1, P1, 1, "M1", "one", MilestoneState.PENDING, NOW, NOW))
    mr.add(Milestone(M2, P2, 1, "M1", "two", MilestoneState.PENDING, NOW, NOW))
    ids = iter(f"id-{n}" for n in range(100))
    repo = SQLiteJobRepository(db, lambda: next(ids))
    jid = JobId.generate()
    repo.add(
        Job(
            jid,
            P1,
            "CODEX_RUN",
            JobState.QUEUED,
            0,
            NOW,
            NOW,
            milestone,
            worker_class=WorkerClass.CODEX,
            max_attempts=max_attempts,
            correlation_id="corr",
        )
    )
    return db, repo, jid


def req(
    jid: JobId, source: JobState, target: JobState, n: int, **kwargs: object
) -> JobTransitionRequest:
    return JobTransitionRequest(
        jid,
        P1,
        source,
        target,
        "reason",
        "SYSTEM",
        "worker",
        "corr",
        NOW + timedelta(seconds=n),
        **kwargs,  # type: ignore[arg-type]
    )


def transition(
    repo: SQLiteJobRepository,
    jid: JobId,
    source: JobState,
    target: JobState,
    n: int,
    **kwargs: object,
) -> Job:
    return repo.apply_transition(req(jid, source, target, n, **kwargs))


def start(repo: SQLiteJobRepository, jid: JobId, n: int) -> None:
    transition(repo, jid, JobState.QUEUED, JobState.DISPATCHED, n)
    transition(repo, jid, JobState.DISPATCHED, JobState.RUNNING, n + 1)


def test_happy_external_and_history(tmp_path: Path) -> None:
    db, repo, jid = setup(tmp_path / "happy.db")
    start(repo, jid, 1)
    transition(repo, jid, JobState.RUNNING, JobState.WAITING_EXTERNAL, 3)
    job = transition(
        repo,
        jid,
        JobState.WAITING_EXTERNAL,
        JobState.SUCCEEDED,
        4,
        result={"ok": True},
        exit_code=0,
    )
    attempts = repo.attempts(jid, P1)
    assert job.state is JobState.SUCCEEDED and job.attempt_number == 1
    assert attempts[0].state.value == "SUCCEEDED" and attempts[0].result == {"ok": True}
    assert [x.new_state for x in repo.transitions(jid, P1)] == [
        JobState.DISPATCHED,
        JobState.RUNNING,
        JobState.WAITING_EXTERNAL,
        JobState.SUCCEEDED,
    ]
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("UPDATE job_attempts SET exit_code=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM job_attempts")
    db.close()


def test_retry_preserves_attempts_and_enforces_limit(tmp_path: Path) -> None:
    db, repo, jid = setup(tmp_path / "retry.db")
    start(repo, jid, 1)
    retry_at = NOW + timedelta(minutes=1)
    transition(
        repo,
        jid,
        JobState.RUNNING,
        JobState.RETRY_WAIT,
        3,
        next_retry_at=retry_at,
        error_id="transient",
    )
    assert repo.get(jid, P1).next_retry_at == retry_at
    transition(repo, jid, JobState.RETRY_WAIT, JobState.QUEUED, 4)
    start(repo, jid, 5)
    job = transition(repo, jid, JobState.RUNNING, JobState.SUCCEEDED, 7)
    assert job.next_retry_at is None and [
        a.attempt_number for a in repo.attempts(jid, P1)
    ] == [1, 2]
    assert [a.state.value for a in repo.attempts(jid, P1)] == [
        "RETRYABLE_FAILURE",
        "SUCCEEDED",
    ]
    with pytest.raises(ValueError):
        transition(repo, jid, JobState.SUCCEEDED, JobState.QUEUED, 8)
    db.close()


def test_retry_exhaustion_and_stale_state(tmp_path: Path) -> None:
    db, repo, jid = setup(tmp_path / "limit.db", max_attempts=1)
    start(repo, jid, 1)
    with pytest.raises(AttemptLimitExhaustedError):
        transition(
            repo,
            jid,
            JobState.RUNNING,
            JobState.RETRY_WAIT,
            3,
            next_retry_at=NOW + timedelta(minutes=1),
            error_id="x",
        )
    assert (
        repo.get(jid, P1).state is JobState.RUNNING
        and repo.attempts(jid, P1)[0].state.value == "RUNNING"
    )
    transition(repo, jid, JobState.RUNNING, JobState.FAILED, 4, error_id="final")
    with pytest.raises(StaleJobStateError):
        transition(repo, jid, JobState.RUNNING, JobState.CANCELLED, 5)
    db.close()


@pytest.mark.parametrize(
    "source",
    [
        JobState.QUEUED,
        JobState.DISPATCHED,
        JobState.RUNNING,
        JobState.WAITING_EXTERNAL,
        JobState.RETRY_WAIT,
    ],
)
def test_cancellation_is_terminal(tmp_path: Path, source: JobState) -> None:
    db, repo, jid = setup(tmp_path / f"cancel-{source}.db")
    if source is not JobState.QUEUED:
        transition(repo, jid, JobState.QUEUED, JobState.DISPATCHED, 1)
    if source in {JobState.RUNNING, JobState.WAITING_EXTERNAL, JobState.RETRY_WAIT}:
        transition(repo, jid, JobState.DISPATCHED, JobState.RUNNING, 2)
    if source is JobState.WAITING_EXTERNAL:
        transition(repo, jid, JobState.RUNNING, source, 3)
    if source is JobState.RETRY_WAIT:
        transition(
            repo,
            jid,
            JobState.RUNNING,
            source,
            3,
            next_retry_at=NOW + timedelta(minutes=1),
            error_id="x",
        )
    assert (
        transition(repo, jid, source, JobState.CANCELLED, 4).state is JobState.CANCELLED
    )
    with pytest.raises(ValueError):
        transition(repo, jid, JobState.CANCELLED, JobState.DISPATCHED, 5)
    db.close()


def test_abandonment_is_separate_atomic_terminal_operation(tmp_path: Path) -> None:
    db, repo, jid = setup(tmp_path / "abandoned.db")
    start(repo, jid, 1)
    job = repo.abandon(
        req(
            jid,
            JobState.RUNNING,
            JobState.ABANDONED,
            3,
            metadata={"classification": "lost"},
        )
    )
    assert (
        job.state is JobState.ABANDONED
        and repo.attempts(jid, P1)[0].state.value == "ABANDONED"
    )
    assert repo.transitions(jid, P1)[-1].metadata == {"classification": "lost"}
    with pytest.raises(TerminalJobMutationError):
        repo.abandon(req(jid, JobState.ABANDONED, JobState.ABANDONED, 4))
    db.close()


def test_parent_isolation_and_atomic_history_failure(tmp_path: Path) -> None:
    db, repo, jid = setup(tmp_path / "isolation.db", milestone=None)
    assert repo.get(jid, P1).milestone_id is None
    with pytest.raises(JobProjectMismatchError):
        repo.get(jid, P2)
    with pytest.raises(JobMilestoneProjectMismatchError):
        repo.add(Job(JobId.generate(), P1, "bad", JobState.QUEUED, 0, NOW, NOW, M2))
    transition(repo, jid, JobState.QUEUED, JobState.DISPATCHED, 1)
    db.execute(
        "CREATE TRIGGER fail_job_history BEFORE INSERT ON state_transitions WHEN NEW.entity_type='JOB' BEGIN SELECT RAISE(ABORT,'forced'); END"
    )
    with pytest.raises(PersistenceError):
        transition(repo, jid, JobState.DISPATCHED, JobState.RUNNING, 2)
    assert (
        repo.get(jid, P1).state is JobState.DISPATCHED and repo.attempts(jid, P1) == ()
    )
    db.close()


def test_attempt_finalisation_and_job_success_are_atomic(tmp_path: Path) -> None:
    db, repo, jid = setup(tmp_path / "finalise-atomic.db")
    start(repo, jid, 1)
    db.execute(
        "CREATE TRIGGER fail_success_history BEFORE INSERT ON state_transitions "
        "WHEN NEW.entity_type='JOB' AND NEW.new_state='SUCCEEDED' "
        "BEGIN SELECT RAISE(ABORT,'forced'); END"
    )
    with pytest.raises(PersistenceError):
        transition(repo, jid, JobState.RUNNING, JobState.SUCCEEDED, 3)
    assert repo.get(jid, P1).state is JobState.RUNNING
    assert repo.attempts(jid, P1)[0].state.value == "RUNNING"
    db.close()


def test_upgrade_from_m8_preserves_existing_history(tmp_path: Path) -> None:
    db = open_database(tmp_path / "upgrade.db")
    apply_migrations(db, MIGRATIONS[:3])
    projects = SQLiteProjectRepository(db, lambda: "old")
    projects.add(Project(P1, "one", ProjectState.NEW, NOW, NOW))
    from syntra_build.domain import ProjectTransitionRequest

    projects.apply_transition(
        ProjectTransitionRequest(
            P1,
            ProjectState.NEW,
            ProjectState.DESIGNING,
            "old",
            "SYSTEM",
            None,
            "corr",
            NOW,
        )
    )
    apply_migrations(db)
    assert current_schema_version(db) == 4 and len(projects.transitions(P1)) == 1
    db.close()
