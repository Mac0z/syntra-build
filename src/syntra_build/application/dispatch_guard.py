"""Scheduler dispatch policy seams."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from syntra_build.application.operational_health import ResourceSampler
from syntra_build.domain.health import HealthReason
from syntra_build.domain.jobs import Job, WorkerClass
from syntra_build.infrastructure.config.models import ResourceThresholdConfig

_IMPLEMENTATION_WORKERS = frozenset(
    {
        WorkerClass.ARCHITECT,
        WorkerClass.CODEX,
        WorkerClass.GIT,
        WorkerClass.GITHUB,
        WorkerClass.CI,
    }
)


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    allowed: bool
    reason: HealthReason | None = None


class DispatchGuard(Protocol):
    def evaluate(self, job: Job) -> DispatchDecision: ...


class AllowAllDispatchGuard:
    def evaluate(self, job: Job) -> DispatchDecision:
        return DispatchDecision(True)


class DiskDispatchGuard:
    def __init__(self, sampler: ResourceSampler, thresholds: ResourceThresholdConfig):
        self._sampler, self._thresholds = sampler, thresholds

    def evaluate(self, job: Job) -> DispatchDecision:
        free = self._sampler.sample().free_percent
        if free < self._thresholds.disk_critical_percent_free:
            return DispatchDecision(
                job.worker_class not in _IMPLEMENTATION_WORKERS,
                HealthReason.DISK_CRITICAL,
            )
        if (
            free < self._thresholds.disk_stop_codex_percent_free
            and job.worker_class is WorkerClass.CODEX
        ):
            return DispatchDecision(False, HealthReason.DISK_CODEX_STOP)
        return DispatchDecision(True)
