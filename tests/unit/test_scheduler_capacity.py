from __future__ import annotations

import pytest

from syntra_build.application.scheduler import (
    CapacityLeaseError,
    InvalidSchedulerConfigurationError,
    SchedulerCapacityExhaustedError,
    WorkerCapacity,
)
from syntra_build.domain import WorkerClass


def limits(value: int = 1) -> dict[WorkerClass, int]:
    return dict.fromkeys(WorkerClass, value)


def test_capacity_is_independent_bounded_and_lease_owned() -> None:
    capacity = WorkerCapacity(limits())
    lease = capacity.acquire(WorkerClass.CODEX)

    assert capacity.in_use(WorkerClass.CODEX) == 1
    assert capacity.available(WorkerClass.CODEX) == 0
    assert capacity.available(WorkerClass.ARCHITECT) == 1
    with pytest.raises(SchedulerCapacityExhaustedError):
        capacity.acquire(WorkerClass.CODEX)

    lease.release()
    assert capacity.available(WorkerClass.CODEX) == 1
    with pytest.raises(CapacityLeaseError):
        lease.release()


def test_capacity_requires_positive_limit_for_every_worker_class() -> None:
    incomplete = limits()
    del incomplete[WorkerClass.INTERNAL]
    with pytest.raises(InvalidSchedulerConfigurationError, match="every worker"):
        WorkerCapacity(incomplete)

    invalid = limits()
    invalid[WorkerClass.CODEX] = 0
    with pytest.raises(InvalidSchedulerConfigurationError, match="positive"):
        WorkerCapacity(invalid)
