"""Provider-neutral operational health contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HealthState(StrEnum):
    STARTING = "STARTING"
    RECOVERING = "RECOVERING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    UNHEALTHY = "UNHEALTHY"


class HealthReason(StrEnum):
    DISK_WARNING = "DISK_WARNING"
    DISK_CODEX_STOP = "DISK_CODEX_STOP"
    DISK_CRITICAL = "DISK_CRITICAL"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    RECOVERY_FAILED = "RECOVERY_FAILED"


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    free_percent: float
    artifact_bytes: int = 0


@dataclass(frozen=True, slots=True)
class HealthProjection:
    state: HealthState
    ready: bool
    reasons: tuple[HealthReason, ...]
    resources: ResourceSnapshot
