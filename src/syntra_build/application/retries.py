"""Trusted retry promotion, failure handling, and exhaustion notification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from syntra_build.domain.failures import (
    FailureClassification,
    RetryBackoffPolicy,
    RetryDecision,
    decide_retry,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.job_state_machine import JobTransitionRequest
from syntra_build.domain.jobs import Job, JobState
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import Milestone, MilestoneState


class RetryJobRepository(Protocol):
    def due_retries(self, now: datetime, limit: int = 100) -> tuple[Job, ...]: ...
    def apply_transition(self, request: JobTransitionRequest) -> Job: ...


class MilestoneRepository(Protocol):
    def get(
        self, milestone_id: MilestoneId, project_id: ProjectId | None = None
    ) -> Milestone: ...
    def apply_transition(self, request: MilestoneTransitionRequest) -> Milestone: ...


@dataclass(frozen=True, slots=True)
class ExhaustionNotice:
    project_id: ProjectId
    milestone_id: MilestoneId
    milestone_code: str
    job_type: str
    attempts: int
    classification: FailureClassification
    reason: str
    human_action_required: bool = True

    def message(self) -> str:
        return (
            f"Project {self.project_id} {self.milestone_code} is blocked.\n"
            f"{self.job_type} failed after {self.attempts} infrastructure attempts.\n"
            f"The last failure was classified as {self.classification.value.lower()}; "
            "the retry limit is exhausted.\n"
            "Human intervention is required before Syntra continues."
        )


class ExhaustionNotifier(Protocol):
    def notify(self, notice: ExhaustionNotice) -> None: ...


def retry_transition(
    job: Job,
    classification: FailureClassification,
    failure_time: datetime,
    policy: RetryBackoffPolicy,
    *,
    error_id: str,
) -> tuple[JobTransitionRequest, RetryDecision]:
    decision = decide_retry(
        classification, job.attempt_number, job.max_attempts, failure_time, policy
    )
    target = JobState.RETRY_WAIT if decision.should_retry else JobState.FAILED
    request = JobTransitionRequest(
        job.id,
        job.project_id,
        job.state,
        target,
        "classified execution failure",
        "SYSTEM",
        "retry-policy",
        job.correlation_id,
        failure_time,
        next_retry_at=decision.next_retry_at,
        error_id=error_id,
        failure_classification=classification,
        retry_exhausted=decision.exhausted,
        exhaustion_reason=(
            "infrastructure retry limit exhausted" if decision.exhausted else None
        ),
        metadata={
            "failure_classification": classification.value,
            "retry_exhausted": decision.exhausted,
        },
    )
    return request, decision


def promote_due_retries(
    repository: RetryJobRepository, now: datetime, *, limit: int = 100
) -> tuple[Job, ...]:
    promoted: list[Job] = []
    for job in repository.due_retries(now, limit):
        if job.attempt_number >= job.max_attempts:
            repository.apply_transition(
                JobTransitionRequest(
                    job.id,
                    job.project_id,
                    JobState.RETRY_WAIT,
                    JobState.FAILED,
                    "retry budget exhausted before promotion",
                    "SYSTEM",
                    "retry-policy",
                    job.correlation_id,
                    now,
                    error_id=job.last_error_id,
                    failure_classification=job.failure_classification,
                    retry_exhausted=True,
                    exhaustion_reason="infrastructure retry limit exhausted",
                )
            )
            continue
        promoted.append(
            repository.apply_transition(
                JobTransitionRequest(
                    job.id,
                    job.project_id,
                    JobState.RETRY_WAIT,
                    JobState.QUEUED,
                    "durable retry became due",
                    "SYSTEM",
                    "retry-policy",
                    job.correlation_id,
                    now,
                    failure_classification=job.failure_classification,
                )
            )
        )
    return tuple(promoted)


def escalate_exhausted_job(
    job: Job,
    milestones: MilestoneRepository,
    notifier: ExhaustionNotifier,
    occurred_at: datetime,
) -> Milestone | None:
    """Persist the legal BLOCKED transition before the best-effort side effect."""
    if not job.retry_exhausted or job.milestone_id is None:
        return None
    milestone = milestones.get(job.milestone_id, job.project_id)
    if milestone.state in {MilestoneState.BLOCKED, MilestoneState.FAILED}:
        return milestone
    blocked = milestones.apply_transition(
        MilestoneTransitionRequest(
            milestone.id,
            milestone.project_id,
            milestone.state,
            MilestoneState.BLOCKED,
            "infrastructure retry limit exhausted",
            "SYSTEM",
            "retry-policy",
            job.correlation_id,
            occurred_at,
            metadata={
                "job_id": str(job.id),
                "attempts": job.attempt_number,
                "classification": (
                    job.failure_classification or FailureClassification.UNKNOWN
                ).value,
            },
        )
    )
    notice = ExhaustionNotice(
        job.project_id,
        milestone.id,
        milestone.code,
        job.job_type,
        job.attempt_number,
        job.failure_classification or FailureClassification.UNKNOWN,
        job.exhaustion_reason or "retry limit exhausted",
    )
    notifier.notify(notice)
    return blocked
