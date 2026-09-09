from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock

import pytest

from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
    Scheduler,
    WorkerCapacity,
)
from syntra_build.domain import (
    Job,
    JobId,
    JobState,
    JobTransitionRequest,
    Project,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    WorkerClass,
)
from syntra_build.domain.failures import (
    FailureClassification,
    RetryBackoffPolicy,
    TransientFailure,
)
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)
from syntra_build.infrastructure.persistence.jobs import SchedulableJob

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def capacities(**overrides: int) -> WorkerCapacity:
    values = SchedulerConfig(**overrides).worker_class_limits()
    return WorkerCapacity(values)


@dataclass
class RecordingExecutor:
    result: JobExecutionResult = field(
        default_factory=lambda: JobExecutionResult(JobExecutionDisposition.SUCCEEDED)
    )
    release: Event | None = None
    started: Event = field(default_factory=Event)
    finished: Event = field(default_factory=Event)
    calls: list[Job] = field(default_factory=list)
    active: int = 0
    maximum_active: int = 0
    lock: Lock = field(default_factory=Lock)

    def execute(self, job: Job) -> JobExecutionResult:
        with self.lock:
            self.calls.append(job)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        self.started.set()
        if self.release is not None:
            self.release.wait()
        with self.lock:
            self.active -= 1
        self.finished.set()
        return self.result


class RaisingExecutor:
    def __init__(self, error: Exception | None = None):
        self.error = error or RuntimeError("synthetic executor failure")

    def execute(self, job: Job) -> JobExecutionResult:
        del job
        raise self.error


def database(
    path: Path, state: ProjectState = ProjectState.BUILDING
) -> tuple[sqlite3.Connection, SQLiteProjectRepository, SQLiteJobRepository, ProjectId]:
    connection = open_database(path)
    apply_migrations(connection)
    ids = iter(f"history-{index}" for index in range(1000))
    projects = SQLiteProjectRepository(connection, lambda: next(ids))
    jobs = SQLiteJobRepository(connection, lambda: next(ids))
    project_id = ProjectId.generate()
    projects.add(Project(project_id, "project", state, NOW, NOW))
    return connection, projects, jobs, project_id


def add_job(
    jobs: SQLiteJobRepository,
    project_id: ProjectId,
    *,
    worker_class: WorkerClass = WorkerClass.CODEX,
    state: JobState = JobState.QUEUED,
    priority: int = 0,
    created_at: datetime = NOW,
    scheduled_at: datetime | None = None,
    job_id: JobId | None = None,
    max_attempts: int = 2,
) -> JobId:
    job_id = job_id or JobId.generate()
    jobs.add(
        Job(
            job_id,
            project_id,
            "TEST_JOB",
            state,
            0,
            created_at,
            created_at,
            priority=priority,
            scheduled_at=scheduled_at,
            worker_class=worker_class,
            correlation_id=f"correlation-{job_id}",
            max_attempts=max_attempts,
        )
    )
    return job_id


def transition(
    jobs: SQLiteJobRepository,
    job_id: JobId,
    project_id: ProjectId,
    source: JobState,
    target: JobState,
    when: datetime = NOW,
    **details: object,
) -> Job:
    return jobs.apply_transition(
        JobTransitionRequest(
            job_id,
            project_id,
            source,
            target,
            "test",
            "SYSTEM",
            "test",
            "test-correlation",
            when,
            **details,  # type: ignore[arg-type]
        )
    )


def harvest(scheduler: Scheduler, executor: RecordingExecutor) -> None:
    assert executor.finished.wait(2)
    scheduler.wait_for_wake(2)
    scheduler.run_once()


def test_eligibility_filters_state_and_due_time(tmp_path: Path) -> None:
    connection, _, jobs, project_id = database(tmp_path / "eligibility.db")
    due = add_job(jobs, project_id)
    future = add_job(jobs, project_id, scheduled_at=NOW + timedelta(minutes=1))
    excluded: list[JobId] = []
    for state in JobState:
        if state is JobState.QUEUED:
            continue
        excluded.append(add_job(jobs, project_id, state=state))

    candidates = jobs.eligible(NOW)

    assert [item.job.id for item in candidates] == [due]
    assert future not in [item.job.id for item in candidates]
    assert not set(excluded).intersection(item.job.id for item in candidates)
    connection.close()


def test_eligibility_has_stable_priority_schedule_creation_and_id_order(
    tmp_path: Path,
) -> None:
    connection, _, jobs, project_id = database(tmp_path / "ordering.db")
    oldest = NOW - timedelta(minutes=2)
    first_id = JobId.from_string("00000000-0000-0000-0000-000000000001")
    second_id = JobId.from_string("00000000-0000-0000-0000-000000000002")
    low = add_job(jobs, project_id, priority=0, created_at=oldest)
    scheduled = add_job(
        jobs,
        project_id,
        priority=1,
        created_at=oldest,
        scheduled_at=NOW - timedelta(minutes=1),
    )
    first = add_job(jobs, project_id, priority=1, job_id=first_id)
    second = add_job(jobs, project_id, priority=1, job_id=second_id)

    ordered = [candidate.job.id for candidate in jobs.eligible(NOW)]

    assert ordered == [scheduled, first, second, low]
    connection.close()


def test_codex_limit_and_independent_architect_capacity(tmp_path: Path) -> None:
    connection, _, jobs, project_id = database(tmp_path / "capacity.db")
    codex_ids = [add_job(jobs, project_id) for _ in range(3)]
    architect_id = add_job(jobs, project_id, worker_class=WorkerClass.ARCHITECT)
    release = Event()
    codex = RecordingExecutor(release=release)
    architect = RecordingExecutor(release=release)
    capacity = capacities(codex_concurrency=2, architect_concurrency=1)
    scheduler = Scheduler(
        jobs,
        capacity,
        {WorkerClass.CODEX: codex, WorkerClass.ARCHITECT: architect},
        clock=lambda: NOW,
    )

    result = scheduler.run_once()

    assert result.dispatched == 3 and result.skipped_capacity == 1
    assert capacity.in_use(WorkerClass.CODEX) == 2
    assert capacity.in_use(WorkerClass.ARCHITECT) == 1
    assert (
        sum(
            jobs.get(job_id, project_id).state is JobState.QUEUED
            for job_id in codex_ids
        )
        == 1
    )
    assert jobs.get(architect_id, project_id).state is JobState.RUNNING
    release.set()
    assert codex.finished.wait(2) and architect.finished.wait(2)
    scheduler.wait_for_wake(2)
    scheduler.run_once()
    assert codex.maximum_active == 2
    scheduler.close()
    connection.close()


@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        (JobExecutionDisposition.WAITING_EXTERNAL, JobState.WAITING_EXTERNAL),
        (JobExecutionDisposition.SUCCEEDED, JobState.SUCCEEDED),
        (JobExecutionDisposition.FAILED, JobState.FAILED),
        (JobExecutionDisposition.CANCELLED, JobState.CANCELLED),
    ],
)
def test_non_local_and_terminal_outcomes_release_capacity(
    tmp_path: Path,
    disposition: JobExecutionDisposition,
    expected: JobState,
) -> None:
    connection, _, jobs, project_id = database(tmp_path / f"release-{expected}.db")
    job_id = add_job(jobs, project_id)
    executor = RecordingExecutor(JobExecutionResult(disposition))
    capacity = capacities(codex_concurrency=1)
    scheduler = Scheduler(
        jobs, capacity, {WorkerClass.CODEX: executor}, clock=lambda: NOW
    )

    scheduler.run_once()
    harvest(scheduler, executor)

    assert jobs.get(job_id, project_id).state is expected
    assert capacity.available(WorkerClass.CODEX) == 1
    scheduler.close()
    connection.close()


def test_worker_exception_is_durable_and_does_not_leak_slot(tmp_path: Path) -> None:
    connection, _, jobs, project_id = database(tmp_path / "worker-failure.db")
    job_id = add_job(jobs, project_id)
    capacity = capacities(codex_concurrency=1)
    scheduler = Scheduler(
        jobs, capacity, {WorkerClass.CODEX: RaisingExecutor()}, clock=lambda: NOW
    )

    scheduler.run_once()
    scheduler.wait_for_wake(2)
    result = scheduler.run_once()

    assert result.worker_start_failures == 1
    assert jobs.get(job_id, project_id).state is JobState.FAILED
    assert len(jobs.attempts(job_id, project_id)) == 1
    assert capacity.available(WorkerClass.CODEX) == 1
    scheduler.close()
    connection.close()


def test_executor_retry_wait_cannot_bypass_central_timing(tmp_path: Path) -> None:
    connection, _, jobs, project_id = database(tmp_path / "no-bypass.db")
    job_id = add_job(jobs, project_id)
    executor = RecordingExecutor(
        JobExecutionResult(
            JobExecutionDisposition.RETRY_WAIT,
            error_id="executor-requested-retry",
            next_retry_at=NOW + timedelta(days=99),
            failure_classification=FailureClassification.TRANSIENT,
        )
    )
    scheduler = Scheduler(
        jobs,
        capacities(codex_concurrency=1),
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
        retry_policy=RetryBackoffPolicy((30.0,), 0),
    )
    scheduler.run_once()
    harvest(scheduler, executor)
    persisted = jobs.get(job_id, project_id)
    assert persisted.state is JobState.RETRY_WAIT
    assert persisted.next_retry_at == NOW + timedelta(seconds=30)
    scheduler.close()
    connection.close()


@pytest.mark.parametrize(
    ("max_attempts", "expected"), [(2, JobState.RETRY_WAIT), (1, JobState.FAILED)]
)
def test_typed_transient_exception_uses_exhaustion_path(
    tmp_path: Path, max_attempts: int, expected: JobState
) -> None:
    connection, _, jobs, project_id = database(
        tmp_path / f"raised-transient-{max_attempts}.db"
    )
    job_id = add_job(jobs, project_id, max_attempts=max_attempts)
    exhausted: list[Job] = []
    scheduler = Scheduler(
        jobs,
        capacities(codex_concurrency=1),
        {WorkerClass.CODEX: RaisingExecutor(TransientFailure("temporary"))},
        clock=lambda: NOW,
        retry_policy=RetryBackoffPolicy((5.0,), 0),
        exhaustion_handler=lambda job, _at: exhausted.append(job),
    )
    scheduler.run_once()
    scheduler.wait_for_wake(2)
    scheduler.run_once()
    persisted = jobs.get(job_id, project_id)
    assert persisted.state is expected
    assert persisted.attempt_number == 1
    assert len(jobs.attempts(job_id, project_id)) == 1
    assert persisted.retry_exhausted is (max_attempts == 1)
    assert len(exhausted) == (1 if max_attempts == 1 else 0)
    scheduler.close()
    connection.close()


@dataclass
class ClaimInterceptor:
    jobs: SQLiteJobRepository
    before_claim: Callable[[], None]
    called: bool = False

    def eligible(
        self, now: datetime, limit: int | None = None
    ) -> tuple[SchedulableJob, ...]:
        return self.jobs.eligible(now, limit)

    def claim_for_dispatch(
        self,
        request: JobTransitionRequest,
        allowed_project_states: Sequence[ProjectState],
    ) -> Job:
        if not self.called:
            self.called = True
            self.before_claim()
        return self.jobs.claim_for_dispatch(request, allowed_project_states)

    def apply_transition(self, request: JobTransitionRequest) -> Job:
        return self.jobs.apply_transition(request)


def test_stale_claim_does_not_start_worker_or_leak_reservation(tmp_path: Path) -> None:
    connection, _, jobs, project_id = database(tmp_path / "stale-claim.db")
    job_id = add_job(jobs, project_id)

    def competing_claim() -> None:
        jobs.claim_for_dispatch(
            JobTransitionRequest(
                job_id,
                project_id,
                JobState.QUEUED,
                JobState.DISPATCHED,
                "competing scheduler",
                "SYSTEM",
                "competitor",
                "stale-claim",
                NOW,
            ),
            (ProjectState.BUILDING,),
        )

    executor = RecordingExecutor()
    capacity = capacities()
    scheduler = Scheduler(
        ClaimInterceptor(jobs, competing_claim),
        capacity,
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )

    result = scheduler.run_once()

    assert result.stale_claims == 1 and result.dispatched == 0
    assert not executor.calls
    assert capacity.in_use(WorkerClass.CODEX) == 0
    assert jobs.get(job_id, project_id).state is JobState.DISPATCHED
    scheduler.close()
    connection.close()


@pytest.mark.parametrize("target", [ProjectState.PAUSED, ProjectState.CANCELLED])
def test_project_state_is_revalidated_at_claim_boundary(
    tmp_path: Path, target: ProjectState
) -> None:
    connection, projects, jobs, project_id = database(tmp_path / f"race-{target}.db")
    job_id = add_job(jobs, project_id)

    def change_project() -> None:
        projects.apply_transition(
            ProjectTransitionRequest(
                project_id,
                ProjectState.BUILDING,
                target,
                "test race",
                "SYSTEM",
                "test",
                "project-race",
                NOW,
            )
        )

    executor = RecordingExecutor()
    scheduler = Scheduler(
        ClaimInterceptor(jobs, change_project),
        capacities(),
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )

    result = scheduler.run_once()

    assert result.skipped_project_state == 1
    assert not executor.calls
    assert jobs.get(job_id, project_id).state is JobState.QUEUED
    scheduler.close()
    connection.close()


def test_waiting_human_blocks_codex_but_allows_control_work(tmp_path: Path) -> None:
    connection, _, jobs, project_id = database(
        tmp_path / "waiting-human.db", ProjectState.WAITING_HUMAN
    )
    codex_id = add_job(jobs, project_id)
    message_id = add_job(jobs, project_id, worker_class=WorkerClass.MESSAGING)
    executor = RecordingExecutor()
    scheduler = Scheduler(
        jobs,
        capacities(),
        {WorkerClass.CODEX: executor, WorkerClass.MESSAGING: executor},
        clock=lambda: NOW,
    )

    result = scheduler.run_once()
    harvest(scheduler, executor)

    assert result.skipped_project_state == 1 and result.dispatched == 1
    assert jobs.get(codex_id, project_id).state is JobState.QUEUED
    assert jobs.get(message_id, project_id).state is JobState.SUCCEEDED
    scheduler.close()
    connection.close()


def test_drain_keeps_queue_and_active_work_can_finish(tmp_path: Path) -> None:
    connection, _, jobs, project_id = database(tmp_path / "drain.db")
    first = add_job(jobs, project_id)
    second = add_job(jobs, project_id)
    release = Event()
    executor = RecordingExecutor(release=release)
    scheduler = Scheduler(
        jobs,
        capacities(codex_concurrency=1),
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )
    scheduler.run_once()
    running = next(
        job_id
        for job_id in (first, second)
        if jobs.get(job_id, project_id).state is JobState.RUNNING
    )
    queued = second if running == first else first
    scheduler.enter_drain()
    assert scheduler.run_once().dispatched == 0
    assert jobs.get(queued, project_id).state is JobState.QUEUED

    release.set()
    harvest(scheduler, executor)
    assert jobs.get(running, project_id).state is JobState.SUCCEEDED
    assert jobs.get(queued, project_id).state is JobState.QUEUED
    scheduler.exit_drain()
    assert scheduler.run_once().dispatched == 1
    harvest(scheduler, executor)
    scheduler.close()
    connection.close()


def test_restart_rediscovers_only_persisted_queued_jobs(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    connection, _, jobs, project_id = database(path)
    queued = add_job(jobs, project_id)
    dispatched = add_job(jobs, project_id)
    running = add_job(jobs, project_id)
    waiting = add_job(jobs, project_id)
    transition(jobs, dispatched, project_id, JobState.QUEUED, JobState.DISPATCHED)
    transition(jobs, running, project_id, JobState.QUEUED, JobState.DISPATCHED)
    transition(jobs, running, project_id, JobState.DISPATCHED, JobState.RUNNING)
    transition(jobs, waiting, project_id, JobState.QUEUED, JobState.DISPATCHED)
    transition(jobs, waiting, project_id, JobState.DISPATCHED, JobState.RUNNING)
    transition(jobs, waiting, project_id, JobState.RUNNING, JobState.WAITING_EXTERNAL)
    connection.close()

    reopened = open_database(path)
    restart_ids = iter(f"restart-history-{index}" for index in range(10))
    restarted_jobs = SQLiteJobRepository(reopened, lambda: next(restart_ids))
    executor = RecordingExecutor()
    scheduler = Scheduler(
        restarted_jobs,
        capacities(),
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )
    assert scheduler.run_once().dispatched == 1
    harvest(scheduler, executor)

    assert restarted_jobs.get(queued, project_id).state is JobState.SUCCEEDED
    assert restarted_jobs.get(dispatched, project_id).state is JobState.DISPATCHED
    assert restarted_jobs.get(running, project_id).state is JobState.RUNNING
    assert restarted_jobs.get(waiting, project_id).state is JobState.WAITING_EXTERNAL
    scheduler.close()
    reopened.close()


def test_blocking_worker_does_not_block_control_plane_database_read(
    tmp_path: Path,
) -> None:
    connection, projects, jobs, project_id = database(tmp_path / "responsive.db")
    add_job(jobs, project_id)
    release = Event()
    executor = RecordingExecutor(release=release)
    scheduler = Scheduler(
        jobs, capacities(), {WorkerClass.CODEX: executor}, clock=lambda: NOW
    )

    scheduler.run_once()
    assert executor.started.wait(2)
    assert projects.get(project_id).state is ProjectState.BUILDING
    assert scheduler.run_once().considered == 0

    release.set()
    harvest(scheduler, executor)
    scheduler.close()
    connection.close()


def test_equal_priority_codex_capacity_is_shared_between_projects(
    tmp_path: Path,
) -> None:
    db, projects, jobs, project_a = database(tmp_path / "fair.db")
    project_b = ProjectId.generate()
    projects.add(Project(project_b, "project-b", ProjectState.BUILDING, NOW, NOW))
    for _ in range(4):
        add_job(jobs, project_a)
    add_job(jobs, project_b)
    release = Event()
    executor = RecordingExecutor(release=release)
    scheduler = Scheduler(
        jobs,
        capacities(codex_concurrency=2),
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )
    result = scheduler.run_once()
    assert result.dispatched == 2
    release.set()
    scheduler.close()
    assert {call.project_id for call in executor.calls} == {project_a, project_b}
    db.close()


def test_priority_precedes_project_round_robin(tmp_path: Path) -> None:
    db, projects, jobs, project_a = database(tmp_path / "priority-fair.db")
    project_b = ProjectId.generate()
    projects.add(Project(project_b, "project-b", ProjectState.BUILDING, NOW, NOW))
    add_job(jobs, project_a, priority=0)
    add_job(jobs, project_b, priority=100)
    release = Event()
    executor = RecordingExecutor(release=release)
    scheduler = Scheduler(
        jobs,
        capacities(codex_concurrency=1),
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )
    scheduler.run_once()
    release.set()
    scheduler.close()
    assert executor.calls[0].project_id == project_b
    db.close()


def test_round_robin_fairness_persists_across_scheduler_cycles(tmp_path: Path) -> None:
    db, projects, jobs, project_a = database(tmp_path / "fair-cycles.db")
    project_b = ProjectId.generate()
    projects.add(Project(project_b, "project-b", ProjectState.BUILDING, NOW, NOW))
    for _ in range(3):
        add_job(jobs, project_a)
    for _ in range(2):
        add_job(jobs, project_b)
    executor = RecordingExecutor()
    scheduler = Scheduler(
        jobs,
        capacities(codex_concurrency=1),
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )

    scheduler.run_once()
    for _ in range(5):
        scheduler.wait_for_wake(2)
        scheduler.run_once()

    sequence = [job.project_id for job in executor.calls]
    assert len(sequence) == 5
    assert sequence[0] != sequence[1]
    assert sequence[:4] in (
        [project_a, project_b, project_a, project_b],
        [project_b, project_a, project_b, project_a],
    )
    scheduler.close()
    db.close()


def test_rework_job_returns_behind_waiting_project(tmp_path: Path) -> None:
    db, projects, jobs, project_a = database(tmp_path / "fair-rework.db")
    project_b = ProjectId.generate()
    projects.add(Project(project_b, "project-b", ProjectState.BUILDING, NOW, NOW))
    add_job(jobs, project_a, created_at=NOW - timedelta(seconds=2))
    add_job(jobs, project_b, created_at=NOW - timedelta(seconds=1))
    executor = RecordingExecutor()
    scheduler = Scheduler(
        jobs,
        capacities(codex_concurrency=1),
        {WorkerClass.CODEX: executor},
        clock=lambda: NOW,
    )

    scheduler.run_once()
    scheduler.wait_for_wake(2)
    assert executor.calls[0].project_id == project_a
    # Simulate the completed workflow producing another equal-priority Codex cycle.
    # Its older timestamp makes it sort first before project fairness is applied.
    add_job(jobs, project_a, created_at=NOW - timedelta(seconds=3))
    scheduler.run_once()
    scheduler.wait_for_wake(2)

    assert executor.calls[1].project_id == project_b
    scheduler.run_once()
    scheduler.close()
    db.close()
