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
    MissingCIPolicy,
)
from syntra_build.application.pull_requests import GitHubPullRequestGateway
from syntra_build.domain.ci import (
    CI_INTERFACE_VERSION,
    CIDiagnosis,
    CIFailureClassification,
    CIObservation,
    CIOverallStatus,
    CIProgress,
    CIWorkflowPresence,
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
        missing_ci: MissingCIPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.connection, self.github_prs, self.actions = connection, github_prs, actions
        self.policy = required_checks or RequiredCheckPolicy()
        self.missing_ci = missing_ci or MissingCIPolicy()
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
        self._guard(project_id, milestone_id)
        persisted = self.prs.for_milestone(milestone_id)
        if persisted is None or persisted.project_id != project_id:
            raise CIMonitorError("trusted implementation pull request is missing")
        self._guard(project_id, milestone_id, persisted.id)
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
        with transaction(self.connection):
            self._guard(project_id, milestone_id, persisted.id)
            self.prs.save_verified(
                persisted.id, persisted.github_repository_id, live, persisted.title, now
            )
        prior = self.runs.latest_for_head(persisted.id, live.head_sha)
        if prior is not None and prior.overall_status in {
            CIOverallStatus.PASSED,
            CIOverallStatus.FAILED,
            CIOverallStatus.CANCELLED,
        }:
            # A completed check is immutable evidence. Reuse it after restart
            # rather than allowing a later API outage to erase its conclusion.
            final_live = self.github_prs.get(
                repo["full_name"],
                persisted.external_pr_number,
                project_id,
                milestone_id,
            )
            self._verify_live_pr(final_live, persisted, repo["external_repository_id"])
            with transaction(self.connection):
                self._guard(project_id, milestone_id, persisted.id, live.head_sha)
                self.prs.save_verified(
                    persisted.id,
                    persisted.github_repository_id,
                    final_live,
                    persisted.title,
                    now,
                )
                if final_live.head_sha == live.head_sha:
                    if self._retryable_terminal(prior) and prior.next_check_at is None:
                        self._block(
                            prior, project_id, milestone_id, correlation_id, now
                        )
                    else:
                        self._transition(
                            prior, project_id, milestone_id, correlation_id, now
                        )
            if (
                final_live.head_sha == live.head_sha
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
            return prior
        provider_error = None
        observation = None
        try:
            observation = self.actions.observe(
                repo["full_name"], persisted.external_pr_number, live.head_sha
            )
        except CIProviderError as error:
            provider_error = error
        # Missing evidence and provider errors need the same independent head
        # verification as a pass or test failure before a trusted transition.
        final_live = self.github_prs.get(
            repo["full_name"], persisted.external_pr_number, project_id, milestone_id
        )
        self._verify_live_pr(final_live, persisted, repo["external_repository_id"])
        with transaction(self.connection):
            self._guard(project_id, milestone_id, persisted.id, live.head_sha)
            current_head = final_live.head_sha == live.head_sha
            if provider_error is not None:
                record = self._provider_failure(
                    project_id,
                    milestone_id,
                    persisted.id,
                    live.head_sha,
                    correlation_id,
                    provider_error,
                    now,
                    block=current_head,
                )
            else:
                assert observation is not None
                record = self._observation(
                    project_id,
                    milestone_id,
                    persisted,
                    live.head_sha,
                    observation,
                    prior,
                    now,
                )
                if current_head:
                    if record.next_check_at is None and (
                        record.overall_status is CIOverallStatus.UNKNOWN
                        or self._retryable_terminal(record)
                    ):
                        self._block(
                            record, project_id, milestone_id, correlation_id, now
                        )
                    else:
                        self._transition(
                            record, project_id, milestone_id, correlation_id, now
                        )
            if not current_head:
                self.prs.save_verified(
                    persisted.id,
                    persisted.github_repository_id,
                    final_live,
                    persisted.title,
                    now,
                )
        return record

    def _guard(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        pr_id: str | None = None,
        head_sha: str | None = None,
    ) -> None:
        row = self.connection.execute(
            """SELECT p.state AS project_state, m.state AS milestone_state,
            m.active_pull_request_id, pr.head_sha, pr.state AS pr_state
            FROM milestones m JOIN projects p ON p.id=m.project_id
            LEFT JOIN pull_requests pr ON pr.id=m.active_pull_request_id
            WHERE m.id=? AND m.project_id=?""",
            (str(milestone_id), str(project_id)),
        ).fetchone()
        if row is None or row["project_state"] != "BUILDING":
            raise CIMonitorError("CI reconciliation requires a BUILDING project")
        if row["milestone_state"] != "CI_RUNNING":
            raise CIMonitorError("CI reconciliation requires CI_RUNNING")
        if pr_id is not None and (
            row["active_pull_request_id"] != pr_id or row["pr_state"] != "OPEN"
        ):
            raise CIMonitorError("trusted active implementation pull request changed")
        if head_sha is not None and row["head_sha"] != head_sha:
            raise CIMonitorError("persisted PR head changed during CI reconciliation")

    def _observation(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        pr: PullRequestRecord,
        head_sha: str,
        observation: CIObservation,
        prior: CIRunRecord | None,
        now: datetime,
    ) -> CIRunRecord:
        status = self.policy.evaluate(observation.checks)
        if (
            not observation.checks
            and observation.external_workflow_run_ids
            and observation.workflow_status
            in {CIOverallStatus.QUEUED, CIOverallStatus.RUNNING}
        ):
            status = observation.workflow_status
        classification = (
            self.policy.classify(observation.checks)
            if status is CIOverallStatus.FAILED
            else None
        )
        started = prior.started_at if prior is not None else now
        retry_count = prior.retry_count if prior is not None else 0
        unknown_since = None
        unknown_deadline = None
        unknown_count = 0
        diagnosis = None
        diagnosis_summary = None
        next_at = None
        if status is CIOverallStatus.UNKNOWN:
            unknown_since = (prior.unknown_since if prior else None) or now
            unknown_deadline = (
                prior.unknown_deadline_at if prior else None
            ) or self.missing_ci.deadline_at(now)
            unknown_count = prior.unknown_observation_count if prior else 0
            if (
                unknown_count == 0
                or prior is None
                or prior.last_checked_at != now
                or prior.diagnosis
                not in {
                    CIDiagnosis.MISSING_WORKFLOW,
                    CIDiagnosis.NO_APPLICABLE_RUN,
                    CIDiagnosis.NO_REQUIRED_CHECKS,
                    CIDiagnosis.UNRESOLVED_CHECKS,
                }
            ):
                unknown_count += 1
            diagnosis, diagnosis_summary = self._missing_diagnosis(
                pr, head_sha, observation
            )
            if (
                now < unknown_deadline
                and unknown_count < self.missing_ci.max_observations
            ):
                next_at = min(self.polling.next_at(started, now), unknown_deadline)
        elif status in {CIOverallStatus.QUEUED, CIOverallStatus.RUNNING}:
            next_at = self.polling.next_at(started, now)
        elif status is CIOverallStatus.CANCELLED or (
            status is CIOverallStatus.FAILED
            and classification
            in {
                CIFailureClassification.TRANSIENT_INFRASTRUCTURE,
                CIFailureClassification.EXTERNAL_DEPENDENCY,
                CIFailureClassification.UNKNOWN,
            }
        ):
            next_at = self.retries.next_at(retry_count, now)
        return self.runs.reconcile(
            str(project_id),
            str(milestone_id),
            pr.id,
            head_sha,
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
            unknown_since=unknown_since,
            unknown_deadline_at=unknown_deadline,
            unknown_observation_count=unknown_count,
            diagnosis=diagnosis,
            diagnosis_summary=diagnosis_summary,
        )

    @staticmethod
    def _missing_diagnosis(
        pr: PullRequestRecord, head_sha: str, observation: CIObservation
    ) -> tuple[CIDiagnosis, str]:
        if observation.checks:
            diagnosis = CIDiagnosis.UNRESOLVED_CHECKS
            detail = "Required checks have no decisive conclusion; inspect skipped, neutral or unknown jobs."
        elif observation.external_workflow_run_ids:
            diagnosis = CIDiagnosis.NO_REQUIRED_CHECKS
            detail = "Applicable workflow runs produced no required jobs; inspect workflow jobs and conditions."
        elif observation.workflow_presence is CIWorkflowPresence.MISSING:
            diagnosis = CIDiagnosis.MISSING_WORKFLOW
            detail = "No GitHub Actions workflow files exist at this head; restore reviewed CI workflows."
        else:
            diagnosis = CIDiagnosis.NO_APPLICABLE_RUN
            detail = "No applicable pull-request workflow runs exist; check Actions enablement, pull_request triggers, filters and permissions."
        return diagnosis, f"PR #{pr.external_pr_number} at {head_sha}: {detail}"

    @staticmethod
    def _verify_live_pr(live: object, persisted: object, repository_id: int) -> None:
        # Kept in one boundary so initial and pre-transition reads enforce exactly
        # the same repository, PR, branch and open-state identity.
        if not isinstance(live, PullRequestDescriptor) or not isinstance(
            persisted, PullRequestRecord
        ):
            raise CIMonitorError("pull request response type was invalid")
        if (
            live.project_id != persisted.project_id
            or live.milestone_id != persisted.milestone_id
            or live.repository_id != repository_id
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
                self._guard(project_id, milestone_id, pr_id, head_sha)
                self._block(prior, project_id, milestone_id, correlation_id, now)
            return prior
        next_count = prior.retry_count + 1
        next_poll = self.polling.next_at(now, now)
        # Durable intent precedes the external mutation. A restart observes this
        # queued attempt instead of blindly issuing another rerun.
        with transaction(self.connection):
            self._guard(project_id, milestone_id, pr_id, head_sha)
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
        *,
        block: bool = True,
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
        diagnosis, detail = {
            CIProviderFailure.AUTHENTICATION: (
                CIDiagnosis.PROVIDER_AUTHENTICATION,
                "GitHub authentication failed; verify the configured credential and repository access.",
            ),
            CIProviderFailure.REJECTION: (
                CIDiagnosis.PROVIDER_REJECTION,
                "GitHub rejected the CI request; verify repository permissions and request configuration.",
            ),
            CIProviderFailure.MALFORMED: (
                CIDiagnosis.PROVIDER_MALFORMED,
                "GitHub returned malformed CI data; inspect the provider integration before retrying.",
            ),
            CIProviderFailure.TRANSIENT: (
                CIDiagnosis.PROVIDER_UNAVAILABLE,
                "GitHub CI is unavailable or rate limited; check provider availability and rate limits before retrying.",
            ),
        }[error.failure]
        if (
            previous is not None
            and previous.last_checked_at == now
            and previous.diagnosis is diagnosis
        ):
            attempt = previous.retry_count
            next_at = previous.next_check_at
        pr = self.prs.for_milestone(milestone_id)
        assert pr is not None and pr.id == pr_id
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
            unknown_since=previous.unknown_since if previous else None,
            unknown_deadline_at=previous.unknown_deadline_at if previous else None,
            unknown_observation_count=previous.unknown_observation_count
            if previous
            else 0,
            diagnosis=diagnosis,
            diagnosis_summary=f"PR #{pr.external_pr_number} at {head_sha}: {detail}",
        )
        if block and next_at is None:
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
                record.diagnosis_summary
                or f"CI infrastructure retries exhausted for {record.head_sha}; inspect workflow runs and runner availability.",
                "SYSTEM",
                None,
                correlation_id,
                now,
                metadata={
                    "ci_run_id": record.id,
                    "head_sha": record.head_sha,
                    "retry_count": record.retry_count,
                    "diagnosis": record.diagnosis.value if record.diagnosis else None,
                    "unknown_observation_count": record.unknown_observation_count,
                    "unknown_deadline_at": record.unknown_deadline_at.isoformat()
                    if record.unknown_deadline_at
                    else None,
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
            diagnosis=run.diagnosis,
            diagnosis_summary=run.diagnosis_summary,
            next_check_at=run.next_check_at,
        )
