# ruff: noqa: E501
"""One-shot, restart-safe reconciliation of required CI for a trusted M22 PR."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from syntra_build.adapters.github.actions import CIProviderError, CIProviderFailure
from syntra_build.application.ci_policy import RequiredCheckPolicy
from syntra_build.application.ci_polling import (
    AdaptivePollingPolicy,
    InfrastructureRetryPolicy,
)
from syntra_build.application.pull_requests import GitHubPullRequestGateway
from syntra_build.domain.ci import (
    CI_INTERFACE_VERSION,
    CIFailureClassification,
    CIObservation,
    CIOverallStatus,
    CIProgress,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.infrastructure.persistence.ci import CIRunRecord, SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    PullRequestRecord,
    SQLitePullRequestRepository,
)


class CIActionsGateway(Protocol):
    def observe(
        self, repository_full_name: str, pull_request_number: int, head_sha: str
    ) -> CIObservation: ...

    def rerun(self, repository_full_name: str, workflow_run_id: str) -> None: ...


class CIMonitorError(RuntimeError):
    """Trusted PR identity or workflow preconditions were not met."""


class CIMonitor:
    def __init__(
        self,
        connection: sqlite3.Connection,
        github_prs: GitHubPullRequestGateway,
        actions: CIActionsGateway,
        *,
        required_checks: RequiredCheckPolicy | None = None,
        polling: AdaptivePollingPolicy | None = None,
        retries: InfrastructureRetryPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.connection, self.github_prs, self.actions = connection, github_prs, actions
        self.policy = required_checks or RequiredCheckPolicy()
        self.polling, self.retries, self.clock = (
            polling or AdaptivePollingPolicy(),
            retries or InfrastructureRetryPolicy(),
            clock,
        )
        self.runs, self.prs = (
            SQLiteCIRepository(connection),
            SQLitePullRequestRepository(connection),
        )
        self.milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))

    def reconcile(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        correlation_id: str,
        *,
        expected_head_sha: str | None = None,
    ) -> CIRunRecord:
        now = self.clock()
        milestone = self.milestones.get(milestone_id, project_id)
        if milestone.state is not MilestoneState.CI_RUNNING:
            raise CIMonitorError("CI reconciliation requires CI_RUNNING")
        persisted = self.prs.for_milestone(milestone_id)
        if persisted is None or persisted.project_id != project_id:
            raise CIMonitorError("trusted implementation pull request is missing")
        repo = self.connection.execute(
            """SELECT gr.* FROM github_repositories gr
            WHERE gr.id=? AND gr.project_id=? AND gr.status='VERIFIED'""",
            (persisted.github_repository_id, str(project_id)),
        ).fetchone()
        if repo is None:
            raise CIMonitorError("verified repository identity is missing")
        live = self.github_prs.get(
            repo["full_name"], persisted.external_pr_number, project_id, milestone_id
        )
        self._verify_live_pr(live, persisted, repo["external_repository_id"])
        if expected_head_sha is not None and live.head_sha != expected_head_sha:
            raise CIMonitorError("live PR head differs from expected head SHA")
        # M22's repository owns mutable PR observations. This preserves old CI runs,
        # while making the independently fetched live head the current PR head.
        with transaction(self.connection):
            self.prs.save_verified(
                persisted.id, persisted.github_repository_id, live, persisted.title, now
            )
        prior = self.runs.latest_for_head(persisted.id, live.head_sha)
        if (
            prior is not None
            and self._retryable_terminal(prior)
            and prior.next_check_at is not None
            and prior.next_check_at <= now
        ):
            return self._start_rerun(
                project_id,
                milestone_id,
                persisted.id,
                repo["full_name"],
                live.head_sha,
                prior,
                correlation_id,
                now,
            )
        try:
            observation = self.actions.observe(
                repo["full_name"], persisted.external_pr_number, live.head_sha
            )
        except CIProviderError as error:
            return self._provider_failure(
                project_id,
                milestone_id,
                persisted.id,
                live.head_sha,
                correlation_id,
                error,
                now,
            )
        status = self.policy.evaluate(observation.checks)
        classification = (
            self.policy.classify(observation.checks)
            if status is CIOverallStatus.FAILED
            else None
        )
        started = (
            prior.started_at
            if prior is not None and prior.head_sha == live.head_sha
            else now
        )
        retryable_result = status in {
            CIOverallStatus.UNKNOWN,
            CIOverallStatus.CANCELLED,
        } or (
            status is CIOverallStatus.FAILED
            and classification
            in {
                CIFailureClassification.TRANSIENT_INFRASTRUCTURE,
                CIFailureClassification.EXTERNAL_DEPENDENCY,
                CIFailureClassification.UNKNOWN,
            }
        )
        retry_count = prior.retry_count if prior is not None else 0
        next_at = (
            None
            if status
            in {
                CIOverallStatus.PASSED,
                CIOverallStatus.FAILED,
                CIOverallStatus.CANCELLED,
            }
            else self.polling.next_at(started, now)
        )
        if retryable_result:
            next_at = self.retries.next_at(retry_count, now)
        with transaction(self.connection):
            record = self.runs.reconcile(
                str(project_id),
                str(milestone_id),
                persisted.id,
                live.head_sha,
                status,
                observation.checks,
                observation.external_workflow_run_ids,
                classification,
                now,
                retry_count=retry_count,
                next_check_at=next_at,
                summary={
                    "check_count": len(observation.checks),
                    "policy": "all_applicable_actions_jobs",
                },
            )
            if retryable_result and next_at is None:
                self._block(record, project_id, milestone_id, correlation_id, now)
                return record
        # Close the observation-to-transition race with a second independent PR read.
        final_live = self.github_prs.get(
            repo["full_name"], persisted.external_pr_number, project_id, milestone_id
        )
        self._verify_live_pr(final_live, persisted, repo["external_repository_id"])
        if final_live.head_sha != live.head_sha:
            with transaction(self.connection):
                self.prs.save_verified(
                    persisted.id,
                    persisted.github_repository_id,
                    final_live,
                    persisted.title,
                    now,
                )
            return record
        with transaction(self.connection):
            self._transition(record, project_id, milestone_id, correlation_id, now)
        return record

    @staticmethod
    def _verify_live_pr(live: object, persisted: object, repository_id: int) -> None:
        # Kept in one boundary so initial and pre-transition reads enforce exactly
        # the same repository, PR, branch and open-state identity.
        if not isinstance(live, PullRequestDescriptor) or not isinstance(
            persisted, PullRequestRecord
        ):
            raise CIMonitorError("pull request response type was invalid")
        if (
            live.repository_id != repository_id
            or live.pull_request_number != persisted.external_pr_number
            or live.head_branch != persisted.head_branch
            or live.base_branch != persisted.base_branch
        ):
            raise CIMonitorError(
                "live pull request identity differs from persisted ownership"
            )
        if live.state is not PullRequestState.OPEN:
            raise CIMonitorError("implementation pull request is not open")

    @staticmethod
    def _retryable_terminal(record: CIRunRecord) -> bool:
        return record.overall_status in {
            CIOverallStatus.FAILED,
            CIOverallStatus.CANCELLED,
        } and record.failure_classification in {
            CIFailureClassification.TRANSIENT_INFRASTRUCTURE,
            CIFailureClassification.EXTERNAL_DEPENDENCY,
            CIFailureClassification.UNKNOWN,
            None,
        }

    def _start_rerun(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        pr_id: str,
        repository_full_name: str,
        head_sha: str,
        prior: CIRunRecord,
        correlation_id: str,
        now: datetime,
    ) -> CIRunRecord:
        if not prior.external_workflow_run_ids:
            with transaction(self.connection):
                self._block(prior, project_id, milestone_id, correlation_id, now)
            return prior
        next_count = prior.retry_count + 1
        next_poll = self.polling.next_at(now, now)
        # Durable intent precedes the external mutation. A restart observes this
        # queued attempt instead of blindly issuing another rerun.
        with transaction(self.connection):
            record = self.runs.reconcile(
                str(project_id),
                str(milestone_id),
                pr_id,
                head_sha,
                CIOverallStatus.QUEUED,
                (),
                prior.external_workflow_run_ids,
                None,
                now,
                retry_count=next_count,
                next_check_at=next_poll,
                summary={"rerun_of_ci_run_id": prior.id},
                new_attempt=True,
            )
        for workflow_run_id in prior.external_workflow_run_ids:
            self.actions.rerun(repository_full_name, workflow_run_id)
        return record

    def _provider_failure(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        pr_id: str,
        head_sha: str,
        correlation_id: str,
        error: CIProviderError,
        now: datetime,
    ) -> CIRunRecord:
        previous = self.runs.latest_for_head(pr_id, head_sha)
        attempt = (
            previous.retry_count + 1
            if previous and previous.head_sha == head_sha
            else 1
        )
        next_at = self.retries.next_at(attempt - 1, now)
        classification = (
            CIFailureClassification.TRANSIENT_INFRASTRUCTURE
            if error.failure is CIProviderFailure.TRANSIENT
            else CIFailureClassification.UNKNOWN
        )
        with transaction(self.connection):
            record = self.runs.reconcile(
                str(project_id),
                str(milestone_id),
                pr_id,
                head_sha,
                CIOverallStatus.UNKNOWN,
                (),
                (),
                classification,
                now,
                retry_count=attempt,
                next_check_at=next_at,
                summary={"provider_failure": error.failure},
            )
            if next_at is None:
                self._block(record, project_id, milestone_id, correlation_id, now)
        return record

    def _block(
        self,
        record: CIRunRecord,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        correlation_id: str,
        now: datetime,
    ) -> None:
        self.milestones.apply_transition(
            MilestoneTransitionRequest(
                milestone_id,
                project_id,
                MilestoneState.CI_RUNNING,
                MilestoneState.BLOCKED,
                "CI state could not be established after bounded retries",
                "SYSTEM",
                None,
                correlation_id,
                now,
                metadata={
                    "ci_run_id": record.id,
                    "head_sha": record.head_sha,
                    "retry_count": record.retry_count,
                },
            )
        )

    def _transition(
        self,
        record: CIRunRecord,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        correlation_id: str,
        now: datetime,
    ) -> None:
        target = None
        if record.overall_status is CIOverallStatus.PASSED:
            target = MilestoneState.ARCHITECT_REVIEW
        elif (
            record.overall_status is CIOverallStatus.FAILED
            and record.failure_classification
            in {
                CIFailureClassification.IMPLEMENTATION,
                CIFailureClassification.TEST,
                CIFailureClassification.CONFIGURATION,
            }
        ):
            target = MilestoneState.CI_REWORK
        if target is not None:
            failed = ", ".join(
                item.name for item in record.checks if item.failure_summary
            )
            self.milestones.apply_transition(
                MilestoneTransitionRequest(
                    milestone_id,
                    project_id,
                    MilestoneState.CI_RUNNING,
                    target,
                    "required CI passed"
                    if target is MilestoneState.ARCHITECT_REVIEW
                    else "CI requires implementation rework",
                    "SYSTEM",
                    None,
                    correlation_id,
                    now,
                    metadata={
                        "ci_run_id": record.id,
                        "head_sha": record.head_sha,
                        "failure_classification": record.failure_classification.value
                        if record.failure_classification
                        else None,
                        "failed_checks": failed[:500],
                    },
                )
            )

    def progress(self, project_id: ProjectId, milestone_id: MilestoneId) -> CIProgress:
        pr = self.prs.for_milestone(milestone_id)
        run = self.runs.latest_for_head(pr.id, pr.head_sha) if pr is not None else None
        if pr is None or run is None or pr.project_id != project_id:
            raise CIMonitorError("fresh CI progress is unavailable")
        return CIProgress(
            CI_INTERFACE_VERSION,
            project_id,
            milestone_id,
            pr.external_pr_number,
            run.head_sha,
            run.overall_status,
            run.checks,
        )
