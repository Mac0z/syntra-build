"""Provider-neutral failure vocabulary and deterministic retry policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from random import uniform


class FailureClassification(StrEnum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    POLICY = "POLICY"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ClassifiedFailure(Exception):
    classification = FailureClassification.UNKNOWN


class TransientFailure(ClassifiedFailure):
    classification = FailureClassification.TRANSIENT


class ProviderTimeoutFailure(TransientFailure):
    pass


class RateLimitFailure(TransientFailure):
    """A rate limit which the adapter has explicitly declared safe to retry."""


class PermanentFailure(ClassifiedFailure):
    classification = FailureClassification.PERMANENT


class AuthenticationFailure(PermanentFailure):
    pass


class PolicyFailure(ClassifiedFailure):
    classification = FailureClassification.POLICY


class CancelledFailure(ClassifiedFailure):
    classification = FailureClassification.CANCELLED


def classify_failure(error: BaseException) -> FailureClassification:
    """Classify typed failures; unknown exceptions conservatively do not retry."""
    if isinstance(error, ClassifiedFailure):
        return error.classification
    if isinstance(error, TimeoutError):
        return FailureClassification.TRANSIENT
    return FailureClassification.UNKNOWN


@dataclass(frozen=True, slots=True)
class RetryBackoffPolicy:
    """Bounded schedule with injectable multiplicative jitter."""

    schedule_seconds: tuple[float, ...] = (5.0, 30.0, 120.0, 600.0)
    jitter_factor: float = 0.2
    jitter: Callable[[float, float], float] = uniform

    def __post_init__(self) -> None:
        if not self.schedule_seconds or any(x < 0 for x in self.schedule_seconds):
            raise ValueError("retry schedule must contain non-negative delays")
        if any(a > b for a, b in zip(self.schedule_seconds, self.schedule_seconds[1:])):
            raise ValueError("retry schedule must not decrease")
        if not 0 <= self.jitter_factor <= 1:
            raise ValueError("jitter factor must be between zero and one")

    def delay_for(self, attempt_number: int) -> timedelta:
        if type(attempt_number) is not int or attempt_number < 1:
            raise ValueError("attempt number must be positive")
        base = self.schedule_seconds[
            min(attempt_number - 1, len(self.schedule_seconds) - 1)
        ]
        low, high = base * (1 - self.jitter_factor), base * (1 + self.jitter_factor)
        sampled = self.jitter(low, high)
        if not low <= sampled <= high:
            raise ValueError("jitter source returned a value outside its bounds")
        return timedelta(seconds=max(0.0, sampled))


@dataclass(frozen=True, slots=True)
class RetryDecision:
    should_retry: bool
    next_retry_at: datetime | None
    exhausted: bool
    classification: FailureClassification


def decide_retry(
    classification: FailureClassification,
    attempt_number: int,
    max_attempts: int,
    failure_time: datetime,
    policy: RetryBackoffPolicy,
) -> RetryDecision:
    if failure_time.tzinfo is None or failure_time.utcoffset() != UTC.utcoffset(
        failure_time
    ):
        raise ValueError("failure time must be UTC")
    if attempt_number < 1 or max_attempts < 1 or attempt_number > max_attempts:
        raise ValueError("invalid attempt budget")
    retryable = classification is FailureClassification.TRANSIENT
    exhausted = retryable and attempt_number >= max_attempts
    if not retryable or exhausted:
        return RetryDecision(False, None, exhausted, classification)
    return RetryDecision(
        True, failure_time + policy.delay_for(attempt_number), False, classification
    )
