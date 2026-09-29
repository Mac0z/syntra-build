from datetime import UTC, datetime
from uuid import uuid4

import pytest

from syntra_build.application.lifecycle import JobTypeDispatcher
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.domain import Job, JobId, JobState, ProjectId, WorkerClass


def _job(job_type: str = "ARCHITECT_TASK") -> Job:
    now = datetime(2026, 9, 29, tzinfo=UTC)
    return Job(
        JobId.from_string(str(uuid4())),
        ProjectId.from_string(str(uuid4())),
        job_type,
        JobState.QUEUED,
        0,
        now,
        now,
        worker_class=WorkerClass.ARCHITECT,
    )


def test_dispatcher_routes_only_registered_job_type() -> None:
    expected = JobExecutionResult(JobExecutionDisposition.SUCCEEDED)
    dispatcher = JobTypeDispatcher({"ARCHITECT_TASK": lambda _job: expected})
    assert dispatcher.job_types == frozenset({"ARCHITECT_TASK"})
    assert dispatcher.execute(_job()) is expected


def test_dispatcher_fails_closed_for_unknown_job_type() -> None:
    dispatcher = JobTypeDispatcher({"ARCHITECT_TASK": lambda _job: object()})
    with pytest.raises(RuntimeError, match="unsupported trusted job type"):
        dispatcher.execute(_job("USER_CONTROLLED"))
