"""Pure adaptive polling and bounded infrastructure-retry policies."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class MissingCIPolicy:
    """Bound successful observations that have no required CI evidence."""

    grace_seconds: float = 600.0
    max_observations: int = 20

    MAX_GRACE_SECONDS: ClassVar[int] = 86_400

    def __post_init__(self) -> None:
        if (
            isinstance(self.grace_seconds, bool)
            or not isinstance(self.grace_seconds, (int, float))
            or self.grace_seconds <= 0
            or self.grace_seconds > self.MAX_GRACE_SECONDS
            or not isfinite(self.grace_seconds)
        ):
            raise ValueError(
                "grace_seconds must be a finite positive number no greater than "
                f"{self.MAX_GRACE_SECONDS} seconds"
            )
        if type(self.max_observations) is not int or self.max_observations <= 0:
            raise ValueError("max_observations must be a positive integer")

    def deadline_at(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.grace_seconds)


@dataclass(frozen=True, slots=True)
class AdaptivePollingPolicy:
    initial_seconds: float = 20
    medium_seconds: float = 60
    long_seconds: float = 180
    medium_after_seconds: float = 300
    long_after_seconds: float = 1800

    def next_at(self, started_at: datetime, now: datetime) -> datetime:
        age = (now - started_at).total_seconds()
        delay = self.initial_seconds
        if age >= self.long_after_seconds:
            delay = self.long_seconds
        elif age >= self.medium_after_seconds:
            delay = self.medium_seconds
        return now + timedelta(seconds=delay)


@dataclass(frozen=True, slots=True)
class InfrastructureRetryPolicy:
    delays_seconds: tuple[float, ...] = (5, 30, 120, 600)

    def next_at(self, attempt: int, now: datetime) -> datetime | None:
        if attempt >= len(self.delays_seconds):
            return None
        return now + timedelta(seconds=self.delays_seconds[attempt])
