from datetime import UTC, datetime, timedelta
from pathlib import Path

from syntra_build.application.retries import (
    ExhaustionNotice,
    escalate_exhausted_job,
    promote_due_retries,
    retry_transition,
)
from syntra_build.application.scheduler import Scheduler, WorkerCapacity
from syntra_build.domain.failures import FailureClassification, RetryBackoffPolicy
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.job_state_machine import JobTransitionRequest
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.milestones import Milestone, MilestoneState
from syntra_build.domain.projects import Project, ProjectState
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
P1 = ProjectId.from_string("00000000-0000-0000-0000-000000000001")
M1 = MilestoneId.from_string("00000000-0000-0000-0000-000000000011")


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
    assert promote_due_retries(restarted, NOW + timedelta(seconds=6)).promoted == ()
    promoted = promote_due_retries(restarted, NOW + timedelta(seconds=7))
    assert promoted.promoted[0].state is JobState.QUEUED
    assert len(restarted.attempts(job.id, P1)) == 1
    reopened.close()


class PersistedStateNotifier:
    def __init__(
        self, jobs: SQLiteJobRepository, milestones: SQLiteMilestoneRepository
    ) -> None:
        self.jobs = jobs
        self.milestones = milestones
        self.notices: list[ExhaustionNotice] = []

    def notify(self, notice: ExhaustionNotice) -> None:
        assert self.jobs.get(self.job_id, P1).state is JobState.FAILED
        assert self.milestones.get(M1, P1).state is MilestoneState.BLOCKED
        self.notices.append(notice)

    job_id: JobId


def test_exhausted_retry_discovered_after_restart_is_escalated(tmp_path: Path) -> None:
    db = open_database(tmp_path / "exhausted-restart.db")
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "p-history").add(
        Project(P1, "one", ProjectState.BUILDING, NOW, NOW)
    )
    milestones = SQLiteMilestoneRepository(db, lambda: "m-history")
    milestones.add(Milestone(M1, P1, 1, "M1", "one", MilestoneState.CODING, NOW, NOW))
    jobs = SQLiteJobRepository(db, lambda: "j-history")
    job_id = JobId.generate()
    jobs.add(
        Job(
            job_id,
            P1,
            "CODEX",
            JobState.RETRY_WAIT,
            1,
            NOW,
            NOW,
            milestone_id=M1,
            max_attempts=1,
            worker_class=WorkerClass.CODEX,
            next_retry_at=NOW,
            last_error_id="temporary",
            failure_classification=FailureClassification.TRANSIENT,
        )
    )
    notifier = PersistedStateNotifier(jobs, milestones)
    notifier.job_id = job_id

    def handle_exhaustion(job: Job, at: datetime) -> None:
        escalate_exhausted_job(job, milestones, notifier, at)

    scheduler = Scheduler(
        jobs,
        WorkerCapacity({worker: 1 for worker in WorkerClass}),
        {},
        clock=lambda: NOW,
        exhaustion_handler=handle_exhaustion,
    )

    scheduler.run_once()

    exhausted = jobs.get(job_id, P1)
    assert exhausted.state is JobState.FAILED
    assert exhausted.retry_exhausted
    assert jobs.eligible(NOW) == ()
    assert milestones.get(M1, P1).state is MilestoneState.BLOCKED
    assert len(notifier.notices) == 1
    scheduler.run_once()
    assert len(notifier.notices) == 1
    scheduler.close()
    db.close()
