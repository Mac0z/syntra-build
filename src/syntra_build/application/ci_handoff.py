"""Trusted workflow handoff from verified implementation PRs to M23 CI."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from syntra_build.application.ci_scheduler import CIJobCoordinator
from syntra_build.application.pull_requests import PullRequestLifecycleService
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.pull_requests import PullRequestRecord


class PullRequestCIHandoff:
    """Advance only persisted, verified M22 evidence and bootstrap M23 once."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        pull_requests: PullRequestLifecycleService,
    ) -> None:
        self.connection = connection
        self.pull_requests = pull_requests
        self.milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
        self.ci_jobs = CIJobCoordinator(connection)

    def establish(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        change_set_id: str,
        correlation_id: str,
        *,
        commit_message: str | None = None,
        now: datetime | None = None,
    ) -> PullRequestRecord:
        occurred_at = now or datetime.now(UTC)
        record = self.pull_requests.establish(
            project_id,
            milestone_id,
            change_set_id,
            correlation_id,
            commit_message=commit_message,
            now=occurred_at,
        )
        self.accept_verified(record, correlation_id, occurred_at)
        return record

    def accept_verified(
        self,
        record: PullRequestRecord,
        correlation_id: str,
        now: datetime | None = None,
    ) -> None:
        occurred_at = now or datetime.now(UTC)
        with transaction(self.connection):
            persisted = self.connection.execute(
                """SELECT p.*,m.state AS milestone_state
                FROM pull_requests p JOIN milestones m ON m.id=p.milestone_id
                WHERE p.id=? AND p.project_id=? AND p.milestone_id=?
                  AND m.active_pull_request_id=p.id AND p.state='OPEN'""",
                (record.id, str(record.project_id), str(record.milestone_id)),
            ).fetchone()
            if (
                persisted is None
                or persisted["external_pr_number"] != record.external_pr_number
                or persisted["head_sha"] != record.head_sha
            ):
                raise ValueError(
                    "verified pull request evidence is stale or conflicting"
                )
            state = MilestoneState(persisted["milestone_state"])
            if state in {MilestoneState.PR_CREATING, MilestoneState.PUSHING}:
                self.milestones.apply_transition(
                    MilestoneTransitionRequest(
                        record.milestone_id,
                        record.project_id,
                        state,
                        MilestoneState.CI_RUNNING,
                        "verified implementation pull request is ready for CI",
                        "SYSTEM",
                        "pull-request-ci-handoff",
                        correlation_id,
                        occurred_at,
                        metadata={
                            "pull_request_id": record.id,
                            "pull_request_number": record.external_pr_number,
                            "head_sha": record.head_sha,
                        },
                    )
                )
            elif state is not MilestoneState.CI_RUNNING:
                raise ValueError("milestone is not eligible for CI handoff")
            self.ci_jobs.bootstrap(
                record.project_id,
                record.milestone_id,
                correlation_id,
                occurred_at,
            )
