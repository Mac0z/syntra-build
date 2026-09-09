from datetime import UTC, datetime, timedelta

import pytest

from syntra_build.application.retries import retry_transition
from syntra_build.domain.failures import (
    AuthenticationFailure,
    CancelledFailure,
    FailureClassification,
    PolicyFailure,
    ProviderTimeoutFailure,
    RateLimitFailure,
    RetryBackoffPolicy,
    TransientFailure,
    classify_failure,
    decide_retry,
)
from syntra_build.domain.identifiers import JobId, ProjectId
from syntra_build.domain.jobs import Job, JobState

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
P1 = ProjectId.from_string("00000000-0000-0000-0000-000000000001")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TransientFailure(), FailureClassification.TRANSIENT),
        (ProviderTimeoutFailure(), FailureClassification.TRANSIENT),
        (TimeoutError(), FailureClassification.TRANSIENT),
        (RateLimitFailure(), FailureClassification.TRANSIENT),
        (AuthenticationFailure(), FailureClassification.PERMANENT),
        (PolicyFailure(), FailureClassification.POLICY),
        (CancelledFailure(), FailureClassification.CANCELLED),
        (RuntimeError(), FailureClassification.UNKNOWN),
    ],
)
def test_typed_failure_classification(
    error: BaseException, expected: FailureClassification
) -> None:
    assert classify_failure(error) is expected


def test_configured_backoff_and_injected_jitter_boundaries() -> None:
    schedule = (5.0, 30.0, 120.0, 600.0)
    minimum = RetryBackoffPolicy(schedule, 0.2, lambda low, _high: low)
    neutral = RetryBackoffPolicy(schedule, 0.2, lambda low, high: (low + high) / 2)
    maximum = RetryBackoffPolicy(schedule, 0.2, lambda _low, high: high)
    assert [neutral.delay_for(x).total_seconds() for x in range(1, 5)] == list(schedule)
    assert minimum.delay_for(1) == timedelta(seconds=4)
    assert maximum.delay_for(1) == timedelta(seconds=6)
    assert neutral.delay_for(99) == timedelta(seconds=600)


def test_retry_decision_is_utc_bounded_and_conservative() -> None:
    policy = RetryBackoffPolicy(jitter_factor=0)
    first = decide_retry(FailureClassification.TRANSIENT, 1, 4, NOW, policy)
    assert first.should_retry and first.next_retry_at == NOW + timedelta(seconds=5)
    final = decide_retry(FailureClassification.TRANSIENT, 4, 4, NOW, policy)
    assert final.exhausted and not final.should_retry
    unknown = decide_retry(FailureClassification.UNKNOWN, 1, 4, NOW, policy)
    assert not unknown.should_retry and not unknown.exhausted


def test_retry_transition_does_not_change_logical_workflow_counters() -> None:
    job = Job(
        JobId.generate(), P1, "CODEX", JobState.RUNNING, 1, NOW, NOW, max_attempts=4
    )
    request, decision = retry_transition(
        job,
        FailureClassification.TRANSIENT,
        NOW,
        RetryBackoffPolicy(jitter_factor=0),
        error_id="fake-timeout",
    )
    assert decision.should_retry
    assert request.target_state is JobState.RETRY_WAIT
    assert request.next_retry_at == NOW + timedelta(seconds=5)


def test_invalid_jitter_cannot_create_an_invalid_delay() -> None:
    policy = RetryBackoffPolicy(jitter=lambda low, _high: low - 1)
    with pytest.raises(ValueError, match="outside"):
        policy.delay_for(1)
