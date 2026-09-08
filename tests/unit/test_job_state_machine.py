from datetime import UTC, datetime

import pytest

from syntra_build.domain import (
    VALID_JOB_TRANSITIONS,
    JobId,
    JobState,
    JobTransitionRequest,
    ProjectId,
    consumes_worker_capacity,
    validate_job_transition,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.mark.parametrize("source,targets", VALID_JOB_TRANSITIONS.items())
def test_exact_transition_matrix(
    source: JobState, targets: frozenset[JobState]
) -> None:
    for target in JobState:
        if target in targets:
            validate_job_transition(source, target)
        else:
            with pytest.raises(ValueError):
                validate_job_transition(source, target)


def test_external_wait_does_not_consume_worker_capacity() -> None:
    assert consumes_worker_capacity(JobState.RUNNING)
    assert not consumes_worker_capacity(JobState.WAITING_EXTERNAL)
    assert not consumes_worker_capacity(JobState.DISPATCHED)


def test_transition_request_rejects_non_utc_retry_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        JobTransitionRequest(
            JobId.generate(),
            ProjectId.generate(),
            JobState.RUNNING,
            JobState.RETRY_WAIT,
            "retry",
            "SYSTEM",
            None,
            "corr",
            NOW,
            next_retry_at=datetime(2026, 9, 9),
        )
