"""Deterministic health policy and bounded local resource sampling."""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Protocol

from syntra_build.domain.health import (
    HealthProjection,
    HealthReason,
    HealthState,
    ResourceSnapshot,
)
from syntra_build.infrastructure.config.models import ResourceThresholdConfig


class ResourceSampler(Protocol):
    def sample(self) -> ResourceSnapshot: ...


class LocalResourceSampler:
    """Sample the data filesystem; cache the bounded artifact walk."""

    def __init__(self, data_root: Path, *, artifact_ttl_seconds: float = 30.0) -> None:
        self._root = data_root
        self._ttl = artifact_ttl_seconds
        self._cached = (0.0, 0)
        self._lock = Lock()

    def sample(self) -> ResourceSnapshot:
        usage = shutil.disk_usage(self._root)
        return ResourceSnapshot(
            usage.total,
            usage.used,
            usage.free,
            usage.free * 100.0 / usage.total if usage.total else 0.0,
            self._artifact_bytes(),
        )

    def _artifact_bytes(self) -> int:
        now = time.monotonic()
        with self._lock:
            if now - self._cached[0] < self._ttl:
                return self._cached[1]
            total = 0
            root = self._root / "artifacts"
            if root.is_dir():
                for entry in root.rglob("*"):
                    if entry.is_file() and not entry.is_symlink():
                        try:
                            total += entry.stat().st_size
                        except OSError:
                            continue
            self._cached = (now, total)
            return total


class OperationalHealth:
    """Thread-safe lifecycle overlay over resource/database-derived health."""

    def __init__(
        self,
        sampler: ResourceSampler,
        thresholds: ResourceThresholdConfig,
        database_check: Callable[[], bool] = lambda: True,
    ) -> None:
        self._sampler, self._thresholds = sampler, thresholds
        self._database_check = database_check
        self._phase = HealthState.STARTING
        self._lock = Lock()

    def recovering(self) -> None:
        self._set_phase(HealthState.RECOVERING)

    def running(self) -> None:
        self._set_phase(HealthState.HEALTHY)

    def draining(self) -> None:
        self._set_phase(HealthState.DRAINING)

    def recovery_failed(self) -> None:
        self._set_phase(HealthState.UNHEALTHY)

    def _set_phase(self, phase: HealthState) -> None:
        with self._lock:
            self._phase = phase

    def projection(self) -> HealthProjection:
        resources = self._sampler.sample()
        with self._lock:
            phase = self._phase
        if phase in {
            HealthState.STARTING,
            HealthState.RECOVERING,
            HealthState.DRAINING,
        }:
            return HealthProjection(phase, False, (), resources)
        if phase is HealthState.UNHEALTHY:
            return HealthProjection(
                phase, False, (HealthReason.RECOVERY_FAILED,), resources
            )
        try:
            database_ok = self._database_check()
        except sqlite3.Error, OSError:
            database_ok = False
        if not database_ok:
            return HealthProjection(
                HealthState.UNHEALTHY,
                False,
                (HealthReason.DATABASE_UNAVAILABLE,),
                resources,
            )
        free = resources.free_percent
        if free < self._thresholds.disk_critical_percent_free:
            return HealthProjection(
                HealthState.UNHEALTHY, False, (HealthReason.DISK_CRITICAL,), resources
            )
        if free < self._thresholds.disk_stop_codex_percent_free:
            return HealthProjection(
                HealthState.DEGRADED, True, (HealthReason.DISK_CODEX_STOP,), resources
            )
        if free < self._thresholds.disk_warning_percent_free:
            return HealthProjection(
                HealthState.DEGRADED, True, (HealthReason.DISK_WARNING,), resources
            )
        return HealthProjection(HealthState.HEALTHY, True, (), resources)

    def current_health(self) -> str:
        projection = self.projection()
        reasons = ", ".join(reason.value for reason in projection.reasons) or "none"
        readiness = "yes" if projection.ready else "no"
        return (
            f"Health: {projection.state.value}\nReady: {readiness}\nReasons: {reasons}"
        )
