"""Thread-safe in-process capacity leases for the single-service scheduler."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Lock

from syntra_build.application.scheduler.errors import (
    CapacityLeaseError,
    InvalidSchedulerConfigurationError,
    SchedulerCapacityExhaustedError,
)
from syntra_build.domain.jobs import WorkerClass


class WorkerCapacity:
    """Reserve independent, bounded capacity for every M9 worker class."""

    def __init__(self, limits: Mapping[WorkerClass, int]):
        if set(limits) != set(WorkerClass):
            raise InvalidSchedulerConfigurationError(
                "capacity limits must name every worker class exactly once"
            )
        if any(type(value) is not int or value < 1 for value in limits.values()):
            raise InvalidSchedulerConfigurationError(
                "worker capacity limits must be positive integers"
            )
        self._limits = dict(limits)
        self._in_use = dict.fromkeys(WorkerClass, 0)
        self._lock = Lock()

    def capacity(self, worker_class: WorkerClass) -> int:
        return self._limits[worker_class]

    def in_use(self, worker_class: WorkerClass) -> int:
        with self._lock:
            return self._in_use[worker_class]

    def available(self, worker_class: WorkerClass) -> int:
        with self._lock:
            return self._limits[worker_class] - self._in_use[worker_class]

    def acquire(self, worker_class: WorkerClass) -> CapacityLease:
        with self._lock:
            if self._in_use[worker_class] >= self._limits[worker_class]:
                raise SchedulerCapacityExhaustedError(
                    f"{worker_class.value} worker capacity is exhausted"
                )
            self._in_use[worker_class] += 1
        return CapacityLease(self, worker_class)

    def _release(self, worker_class: WorkerClass) -> None:
        with self._lock:
            if self._in_use[worker_class] < 1:
                raise CapacityLeaseError("worker capacity was released without a lease")
            self._in_use[worker_class] -= 1


@dataclass(slots=True)
class CapacityLease:
    """An idempotent owner token; direct manager release is intentionally private."""

    manager: WorkerCapacity
    worker_class: WorkerClass
    _released: bool = field(default=False, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def release(self) -> None:
        with self._lock:
            if self._released:
                raise CapacityLeaseError("worker capacity lease was already released")
            self.manager._release(self.worker_class)
            self._released = True

    def __enter__(self) -> CapacityLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        if not self.released:
            self.release()
