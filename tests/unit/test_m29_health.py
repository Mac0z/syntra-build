from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from syntra_build.application.dispatch_guard import DiskDispatchGuard
from syntra_build.application.operational_health import OperationalHealth
from syntra_build.domain.health import HealthReason, HealthState, ResourceSnapshot
from syntra_build.domain.identifiers import JobId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.infrastructure.config.models import ResourceThresholdConfig


class Sampler:
    def __init__(self, free: float) -> None:
        self.free = free

    def sample(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            1000, 1000 - int(self.free * 10), int(self.free * 10), self.free, 12
        )


@pytest.mark.parametrize(
    ("free", "state", "ready", "reason"),
    [
        (25.0, HealthState.HEALTHY, True, None),
        (15.0, HealthState.DEGRADED, True, HealthReason.DISK_WARNING),
        (7.0, HealthState.DEGRADED, True, HealthReason.DISK_CODEX_STOP),
        (4.0, HealthState.UNHEALTHY, False, HealthReason.DISK_CRITICAL),
    ],
)
def test_disk_health_policy(
    free: float, state: HealthState, ready: bool, reason: HealthReason | None
) -> None:
    health = OperationalHealth(Sampler(free), ResourceThresholdConfig())
    assert health.projection().state is HealthState.STARTING
    health.running()
    projection = health.projection()
    assert (projection.state, projection.ready) == (state, ready)
    assert projection.reasons == (() if reason is None else (reason,))


def test_lifecycle_phases_are_not_ready() -> None:
    health = OperationalHealth(Sampler(50), ResourceThresholdConfig())
    for action, state in (
        (health.recovering, HealthState.RECOVERING),
        (health.draining, HealthState.DRAINING),
        (health.recovery_failed, HealthState.UNHEALTHY),
    ):
        action()
        assert health.projection().state is state
        assert not health.projection().ready


def test_disk_guard_blocks_only_required_workers() -> None:
    sampler = Sampler(7)
    guard = DiskDispatchGuard(sampler, ResourceThresholdConfig())
    now = datetime(2026, 1, 1, tzinfo=UTC)
    codex = Job(
        JobId(UUID("00000000-0000-0000-0000-000000000001")),
        ProjectId(UUID("00000000-0000-0000-0000-000000000002")),
        "CODEX",
        JobState.QUEUED,
        0,
        now,
        now,
        worker_class=WorkerClass.CODEX,
    )
    assert guard.evaluate(codex).reason is HealthReason.DISK_CODEX_STOP
    assert guard.evaluate(replace(codex, worker_class=WorkerClass.MESSAGING)).allowed
    sampler.free = 4
    assert not guard.evaluate(replace(codex, worker_class=WorkerClass.GITHUB)).allowed
    assert guard.evaluate(replace(codex, worker_class=WorkerClass.MESSAGING)).allowed
