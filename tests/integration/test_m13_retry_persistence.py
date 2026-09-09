from datetime import UTC, datetime, timedelta
from pathlib import Path

from syntra_build.application.retries import promote_due_retries, retry_transition
from syntra_build.domain.failures import FailureClassification, RetryBackoffPolicy
from syntra_build.domain.identifiers import JobId, ProjectId
from syntra_build.domain.job_state_machine import JobTransitionRequest
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.projects import Project, ProjectState
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
P1 = ProjectId.from_string("00000000-0000-0000-0000-000000000001")


def request(
    job: Job, source: JobState, target: JobState, at: datetime
) -> JobTransitionRequest:
    return JobTransitionRequest(
        job.id,
        job.project_id,
        source,
        target,
        "test",
        "SYSTEM",
        "test",
        job.correlation_id,
        at,
    )


def test_retry_wait_survives_restart_and_attempt_history_is_separate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "retry.db"
    db = open_database(path)
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "history").add(
        Project(P1, "one", ProjectState.NEW, NOW, NOW)
    )
    repo = SQLiteJobRepository(db, iter(f"id-{x}" for x in range(20)).__next__)
    job = Job(
        JobId.generate(),
        P1,
        "CODEX",
        JobState.QUEUED,
        0,
        NOW,
        NOW,
        max_attempts=4,
        worker_class=WorkerClass.CODEX,
    )
    repo.add(job)
    dispatched = repo.apply_transition(
        request(job, JobState.QUEUED, JobState.DISPATCHED, NOW)
    )
    running = repo.apply_transition(
        request(
            dispatched,
            JobState.DISPATCHED,
            JobState.RUNNING,
            NOW + timedelta(seconds=1),
        )
    )
    failure, _ = retry_transition(
        running,
        FailureClassification.TRANSIENT,
        NOW + timedelta(seconds=2),
        RetryBackoffPolicy(jitter_factor=0),
        error_id="fake-timeout",
    )
    waiting = repo.apply_transition(failure)
    assert waiting.state is JobState.RETRY_WAIT
    assert waiting.attempt_number == 1
    assert waiting.failure_classification is FailureClassification.TRANSIENT
    db.close()

    reopened = open_database(path)
    restarted = SQLiteJobRepository(
        reopened, iter(f"restart-{x}" for x in range(20)).__next__
    )
    assert promote_due_retries(restarted, NOW + timedelta(seconds=6)) == ()
    promoted = promote_due_retries(restarted, NOW + timedelta(seconds=7))
    assert promoted[0].state is JobState.QUEUED
    assert len(restarted.attempts(job.id, P1)) == 1
    reopened.close()
