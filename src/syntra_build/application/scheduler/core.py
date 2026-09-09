"""Bounded scheduler cycles over durable SQLite-backed job repositories."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Event, Lock
from typing import Protocol, cast

from syntra_build.application.retries import (
    RetryJobRepository,
    promote_due_retries,
    retry_transition,
)
from syntra_build.application.scheduler.capacity import CapacityLease, WorkerCapacity
from syntra_build.application.scheduler.errors import (
    ExecutorUnavailableError,
    SchedulerCapacityExhaustedError,
    SchedulerError,
    WorkerStartError,
)
from syntra_build.domain.failures import (
    FailureClassification,
    RetryBackoffPolicy,
    classify_failure,
)
from syntra_build.domain.job_state_machine import JobTransitionRequest
from syntra_build.domain.jobs import Job, JobState, StructuredMetadata, WorkerClass
from syntra_build.domain.projects import ProjectState
from syntra_build.infrastructure.persistence.errors import (
    JobProjectStateIneligibleError,
    PersistenceError,
    StaleJobStateError,
)
from syntra_build.infrastructure.persistence.jobs import SchedulableJob

_LOGGER = logging.getLogger(__name__)


class JobRepository(Protocol):
    def eligible(
        self, now: datetime, limit: int | None = None
    ) -> tuple[SchedulableJob, ...]: ...

    def claim_for_dispatch(
        self,
        request: JobTransitionRequest,
        allowed_project_states: Sequence[ProjectState],
    ) -> Job: ...

    def apply_transition(self, request: JobTransitionRequest) -> Job: ...


class JobExecutor(Protocol):
    """Provider-neutral local execution; implementations do not own job state."""

    def execute(self, job: Job) -> JobExecutionResult: ...


class JobExecutionDisposition(StrEnum):
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class JobExecutionResult:
    disposition: JobExecutionDisposition
    result: StructuredMetadata | None = None
    error_id: str | None = None
    next_retry_at: datetime | None = None
    exit_code: int | None = None
    external_request_id: str | None = None
    process_id: str | None = None
    logs_reference: str | None = None
    failure_classification: FailureClassification | None = None

    @property
    def target_state(self) -> JobState:
        return JobState(self.disposition.value)


@dataclass(frozen=True, slots=True)
class SchedulerCycleResult:
    considered: int = 0
    dispatched: int = 0
    completed: int = 0
    skipped_capacity: int = 0
    skipped_project_state: int = 0
    skipped_executor: int = 0
    stale_claims: int = 0
    worker_start_failures: int = 0
    errors: tuple[SchedulerError, ...] = ()


@dataclass(slots=True)
class _ActiveExecution:
    job: Job
    lease: CapacityLease
    future: Future[JobExecutionResult]


IMPLEMENTATION_WORKERS = frozenset(
    {
        WorkerClass.ARCHITECT,
        WorkerClass.CODEX,
        WorkerClass.GIT,
        WorkerClass.GITHUB,
        WorkerClass.CI,
    }
)
IMPLEMENTATION_PROJECT_STATES = (
    ProjectState.NEW,
    ProjectState.DESIGNING,
    ProjectState.PROVISIONING,
    ProjectState.READY,
    ProjectState.BUILDING,
    ProjectState.COMPLETING,
)
CONTROL_PROJECT_STATES = tuple(ProjectState)


def is_implementation_work(job: Job) -> bool:
    """Apply the conservative M12 worker-class implementation classification."""
    return job.worker_class in IMPLEMENTATION_WORKERS


class Scheduler:
    """One deterministic scheduling cycle plus asynchronously executed local work.

    Repository calls stay on the scheduler/control-plane thread. Worker threads receive
    immutable jobs only, so SQLite connections are never shared across threads.
    """

    def __init__(
        self,
        jobs: JobRepository,
        capacity: WorkerCapacity,
        executors: Mapping[WorkerClass, JobExecutor],
        *,
        clock: Callable[[], datetime] | None = None,
        candidate_limit: int | None = None,
        thread_pool: ThreadPoolExecutor | None = None,
        retry_policy: RetryBackoffPolicy | None = None,
        retry_promotion_limit: int = 100,
        exhaustion_handler: Callable[[Job, datetime], None] | None = None,
    ):
        self._jobs = jobs
        self._capacity = capacity
        self._executors = dict(executors)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._candidate_limit = candidate_limit
        self._pool = thread_pool or ThreadPoolExecutor(
            max_workers=sum(capacity.capacity(item) for item in WorkerClass),
            thread_name_prefix="syntra-worker",
        )
        self._owns_pool = thread_pool is None
        self._active: dict[str, _ActiveExecution] = {}
        self._draining = False
        self._lock = Lock()
        self._wake = Event()
        self._retry_policy = retry_policy or RetryBackoffPolicy()
        self._retry_promotion_limit = retry_promotion_limit
        self._fairness_cursor: dict[tuple[WorkerClass, int], str] = {}
        self._exhaustion_handler = exhaustion_handler

    @property
    def is_draining(self) -> bool:
        with self._lock:
            return self._draining

    def enter_drain(self) -> None:
        with self._lock:
            self._draining = True
        self._wake.set()

    def exit_drain(self) -> None:
        with self._lock:
            self._draining = False
        self._wake.set()

    def run_once(self) -> SchedulerCycleResult:
        """Harvest completed work, then claim as many due jobs as capacity permits."""
        with self._lock:
            completed, failures, errors = self._harvest()
            if hasattr(self._jobs, "due_retries"):
                promote_due_retries(
                    cast(RetryJobRepository, self._jobs),
                    self._now(),
                    limit=self._retry_promotion_limit,
                )
            if self._draining:
                return SchedulerCycleResult(
                    completed=completed,
                    worker_start_failures=failures,
                    errors=tuple(errors),
                )
            candidates = self._jobs.eligible(self._now(), self._candidate_limit)
            counts = {
                "dispatched": 0,
                "skipped_capacity": 0,
                "skipped_project_state": 0,
                "skipped_executor": 0,
                "stale_claims": 0,
            }
            for candidate in self._fair_order(candidates):
                self._dispatch(candidate, counts, errors)
            return SchedulerCycleResult(
                considered=len(candidates),
                completed=completed,
                worker_start_failures=failures,
                errors=tuple(errors),
                **counts,
            )

    def _fair_order(
        self, candidates: Sequence[SchedulableJob]
    ) -> tuple[SchedulableJob, ...]:
        """Apply project round-robin within worker class and priority tier."""
        ordered: list[SchedulableJob] = []
        tiers = sorted(
            {(item.job.worker_class, item.job.priority) for item in candidates},
            key=lambda item: (-item[1], item[0].value),
        )
        for worker_class, priority in tiers:
            tier = [
                x
                for x in candidates
                if x.job.worker_class is worker_class and x.job.priority == priority
            ]
            by_project: dict[str, list[SchedulableJob]] = {}
            project_order: list[str] = []
            for item in tier:
                key = str(item.job.project_id)
                if key not in by_project:
                    by_project[key] = []
                    project_order.append(key)
                by_project[key].append(item)
            cursor = self._fairness_cursor.get((worker_class, priority))
            if cursor in project_order:
                pivot = project_order.index(cursor) + 1
                project_order = project_order[pivot:] + project_order[:pivot]
            while any(by_project.values()):
                for project in project_order:
                    if by_project[project]:
                        ordered.append(by_project[project].pop(0))
            if project_order:
                # Dispatch updates this to the actual last recipient; this fallback
                # only stabilises ordering when every candidate is skipped.
                self._fairness_cursor.setdefault(
                    (worker_class, priority), project_order[-1]
                )
        return tuple(ordered)

    def _dispatch(
        self,
        candidate: SchedulableJob,
        counts: dict[str, int],
        errors: list[SchedulerError],
    ) -> None:
        job = candidate.job
        allowed = (
            IMPLEMENTATION_PROJECT_STATES
            if is_implementation_work(job)
            else CONTROL_PROJECT_STATES
        )
        if candidate.project_state not in allowed:
            counts["skipped_project_state"] += 1
            return
        executor = self._executors.get(job.worker_class)
        if executor is None:
            counts["skipped_executor"] += 1
            errors.append(
                ExecutorUnavailableError(
                    f"no executor registered for {job.worker_class.value}"
                )
            )
            return
        try:
            lease = self._capacity.acquire(job.worker_class)
        except SchedulerCapacityExhaustedError:
            counts["skipped_capacity"] += 1
            return
        try:
            claimed = self._jobs.claim_for_dispatch(
                self._request(job, JobState.QUEUED, JobState.DISPATCHED), allowed
            )
        except JobProjectStateIneligibleError:
            lease.release()
            counts["skipped_project_state"] += 1
            return
        except StaleJobStateError:
            lease.release()
            counts["stale_claims"] += 1
            return
        except Exception:
            lease.release()
            raise

        start_gate = Event()
        may_execute = Event()
        abort = Event()

        def execute() -> JobExecutionResult:
            start_gate.set()
            may_execute.wait()
            if abort.is_set():
                return JobExecutionResult(JobExecutionDisposition.FAILED)
            return executor.execute(claimed)

        future: Future[JobExecutionResult] | None = None
        try:
            future = self._pool.submit(execute)
            start_gate.wait()
            running = self._jobs.apply_transition(
                self._request(claimed, JobState.DISPATCHED, JobState.RUNNING)
            )
            self._active[str(job.id)] = _ActiveExecution(running, lease, future)
            future.add_done_callback(lambda _future: self._wake.set())
            may_execute.set()
            counts["dispatched"] += 1
            self._fairness_cursor[(job.worker_class, job.priority)] = str(
                job.project_id
            )
            self._log(job, "dispatched")
        except Exception:
            abort.set()
            may_execute.set()
            if future is not None and not future.done():
                future.cancel()
            try:
                self._jobs.apply_transition(
                    self._request(
                        claimed,
                        JobState.DISPATCHED,
                        JobState.FAILED,
                        error_id="scheduler-worker-start-failure",
                    )
                )
            finally:
                lease.release()
            errors.append(WorkerStartError("worker could not start"))

    def _harvest(self) -> tuple[int, int, list[SchedulerError]]:
        completed = failures = 0
        errors: list[SchedulerError] = []
        for key, active in tuple(self._active.items()):
            if not active.future.done():
                continue
            try:
                outcome = active.future.result()
                if (
                    outcome.disposition is JobExecutionDisposition.FAILED
                    and outcome.failure_classification is not None
                ):
                    request, _ = retry_transition(
                        active.job,
                        outcome.failure_classification,
                        self._now(),
                        self._retry_policy,
                        error_id=outcome.error_id or "classified-execution-failure",
                    )
                    persisted = self._jobs.apply_transition(request)
                    if persisted.retry_exhausted and self._exhaustion_handler:
                        self._exhaustion_handler(persisted, self._now())
                else:
                    self._jobs.apply_transition(
                        self._request(
                            active.job,
                            JobState.RUNNING,
                            outcome.target_state,
                            result=outcome.result,
                            error_id=outcome.error_id,
                            next_retry_at=outcome.next_retry_at,
                            exit_code=outcome.exit_code,
                            external_request_id=outcome.external_request_id,
                            process_id=outcome.process_id,
                            logs_reference=outcome.logs_reference,
                            failure_classification=outcome.failure_classification,
                        )
                    )
                completed += 1
                self._log(active.job, outcome.target_state.value.casefold())
            except Exception as error:
                failures += 1
                errors.append(WorkerStartError("worker execution failed"))
                try:
                    classification = classify_failure(error)
                    request, _ = retry_transition(
                        active.job,
                        classification,
                        self._now(),
                        self._retry_policy,
                        error_id="scheduler-worker-execution-failure",
                    )
                    self._jobs.apply_transition(request)
                except PersistenceError:
                    _LOGGER.exception(
                        "Worker failure could not be persisted",
                        extra={"event": "scheduler_worker_persistence_failed"},
                    )
            finally:
                active.lease.release()
                del self._active[key]
        return completed, failures, errors

    def _request(
        self,
        job: Job,
        expected: JobState,
        target: JobState,
        **details: object,
    ) -> JobTransitionRequest:
        return JobTransitionRequest(
            job.id,
            job.project_id,
            expected,
            target,
            "scheduler worker lifecycle",
            "SYSTEM",
            "scheduler",
            job.correlation_id,
            self._now(),
            **details,  # type: ignore[arg-type]
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("scheduler clock must return UTC timestamps")
        return value

    def _log(self, job: Job, decision: str) -> None:
        _LOGGER.info(
            "Scheduler job decision",
            extra={
                "event": "scheduler_job_decision",
                "job_id": str(job.id),
                "project_id": str(job.project_id),
                "metadata": {
                    "worker_class": job.worker_class.value,
                    "decision": decision,
                    "capacity_in_use": self._capacity.in_use(job.worker_class),
                    "capacity_limit": self._capacity.capacity(job.worker_class),
                },
            },
        )

    def wait_for_wake(self, timeout: float) -> None:
        """Cooperatively wait without busy-polling; worker completion wakes early."""
        self._wake.wait(timeout)
        self._wake.clear()

    def wake(self) -> None:
        """Wake a cooperative loop, without changing drain or stop state."""
        self._wake.set()

    def close(self, *, wait: bool = True) -> None:
        """Release thread resources without changing any durable queued job."""
        if self._owns_pool:
            self._pool.shutdown(wait=wait)


class SchedulerLoop:
    """Small stoppable wrapper; policy remains observable through ``run_once``."""

    def __init__(self, scheduler: Scheduler, interval_seconds: float = 1.0):
        if interval_seconds <= 0:
            raise ValueError("scheduler interval must be positive")
        self._scheduler = scheduler
        self._interval = interval_seconds
        self._stopped = Event()

    def run(self) -> None:
        while not self._stopped.is_set():
            self._scheduler.run_once()
            self._scheduler.wait_for_wake(self._interval)

    def stop(self) -> None:
        self._stopped.set()
        self._scheduler.wake()
