"""Pure adaptive polling and bounded infrastructure-retry policies."""

from dataclasses import dataclass
from datetime import datetime, timedelta


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
