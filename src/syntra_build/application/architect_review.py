# ruff: noqa: E501
"""Trusted orchestration for exact-head Architect review and rework."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from syntra_build.application.architect import ArchitectError, ArchitectFailureKind
from syntra_build.application.review_rework import ReviewReworkCoordinator
from syntra_build.domain.design import ARCHITECT_INTERFACE_VERSION, DocumentType
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.domain.reviews import (
    ArchitectReview,
    ArchitectReviewRequest,
    ArchitectReviewVerdict,
    ArchitectReworkTask,
    ReviewFinding,
    ReviewFindingSeverity,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    PullRequestRecord,
    SQLitePullRequestRepository,
)
from syntra_build.infrastructure.persistence.reviews import (
    ReviewRecord,
    SQLiteArchitectReviewRepository,
)


class ReviewContextGateway(Protocol):
    def diff(
        self,
        repository_full_name: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> str: ...


class ReviewPullRequestGateway(Protocol):
    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor: ...


class ReviewArchitectProvider(Protocol):
    provider_name: str
    model: str

    def review(self, request: ArchitectReviewRequest) -> ArchitectReview: ...

    def telemetry(self) -> dict[str, int | str | None]: ...


class ArchitectReviewError(RuntimeError):
    pass


class ArchitectReviewService:
    """Build context from trusted persistence and apply only deterministic policy."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        github_prs: ReviewPullRequestGateway,
        review_context: ReviewContextGateway,
        provider: ReviewArchitectProvider,
        *,
        architect_rework_limit: int = 3,
        reasoning_effort: str = "high",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.connection, self.github_prs, self.review_context, self.provider = (
            connection,
            github_prs,
            review_context,
            provider,
        )
        self.limit, self.reasoning_effort, self.clock, self.id_factory = (
            architect_rework_limit,
            reasoning_effort,
            clock,
            id_factory,
        )
        self.prs = SQLitePullRequestRepository(connection)
        self.milestones = SQLiteMilestoneRepository(connection, id_factory)
        self.reviews = SQLiteArchitectReviewRepository(connection)
        self.rework = ReviewReworkCoordinator(
            connection, clock=clock, id_factory=id_factory
        )

    def review(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        correlation_id: str,
        *,
        expected_head_sha: str | None = None,
    ) -> ReviewRecord:
        completed_review = self.reviews.completed_for_correlation(
            str(project_id), str(milestone_id), correlation_id
        )
        if completed_review is not None:
            return completed_review
        now = self.clock()
        request, pr_id, repository_full_name, persisted = self._build_request(
            project_id, milestone_id, correlation_id, expected_head_sha
        )
        request_id = self.id_factory()
        with transaction(self.connection):
            self.reviews.begin(
                request_id,
                request,
                self.provider.provider_name,
                self.provider.model,
                self.reasoning_effort,
                now,
            )
        try:
            response = self.provider.review(request)
            self._validate_response(request, response)
        except ArchitectError as error:
            with transaction(self.connection):
                self.reviews.fail(request_id, error.kind, self.clock())
            raise
        except Exception as error:
            with transaction(self.connection):
                self.reviews.fail(
                    request_id, ArchitectFailureKind.MALFORMED_RESPONSE, self.clock()
                )
            raise ArchitectReviewError(
                "Architect review response was invalid"
            ) from error
        final_live = self.github_prs.get(
            repository_full_name, persisted.external_pr_number, project_id, milestone_id
        )
        self._verify_live(
            final_live,
            persisted,
            self._repository_external_id(persisted.github_repository_id),
        )
        stale = final_live.head_sha != request.head_sha
        completed = self.clock()
        usage = self.provider.telemetry()
        response_id = usage.get("provider_response_id")
        with transaction(self.connection):
            if stale:
                self.prs.save_verified(
                    persisted.id,
                    persisted.github_repository_id,
                    final_live,
                    persisted.title,
                    completed,
                )
            record = self.reviews.complete(
                request_id,
                pr_id,
                response,
                response_id if isinstance(response_id, str) else None,
                usage,
                completed,
                superseded=stale,
            )
            if stale:
                # The authoritative state machine has no ARCHITECT_REVIEW ->
                # CI_RUNNING edge. Preserve the historical interaction and updated
                # head, but do not invent a transition or accept stale evidence.
                # The existing PR/CI reconciliation path must observe passing CI
                # for this new persisted head before a subsequent review can start.
                pass
            elif response.verdict is ArchitectReviewVerdict.APPROVE:
                # A later explicit APPROVE is the persisted resolution evidence; code
                # movement alone never closes a finding.
                self.connection.execute(
                    """UPDATE architect_review_findings SET status='RESOLVED',resolved_by_review_id=? WHERE status='OPEN' AND review_id IN (SELECT id FROM architect_reviews WHERE project_id=? AND milestone_id=? AND pull_request_id=? AND id<>?)""",
                    (record.id, str(project_id), str(milestone_id), pr_id, record.id),
                )
                self.milestones.apply_transition(
                    MilestoneTransitionRequest(
                        milestone_id,
                        project_id,
                        MilestoneState.ARCHITECT_REVIEW,
                        MilestoneState.MERGE_READY,
                        "current exact-head Architect approval accepted",
                        "SYSTEM",
                        "architect-review",
                        correlation_id,
                        completed,
                        metadata={
                            "review_id": record.id,
                            "reviewed_sha": response.reviewed_sha,
                        },
                    )
                )
            elif response.verdict is ArchitectReviewVerdict.CHANGES_REQUIRED:
                task = ArchitectReworkTask(
                    "REVIEW_REWORK",
                    project_id,
                    milestone_id,
                    persisted.external_pr_number,
                    persisted.head_branch,
                    response.reviewed_sha,
                    response.findings,
                    request.agents_instructions,
                )
                rework_task_id = self.id_factory()
                self.connection.execute(
                    "INSERT INTO architect_rework_tasks VALUES (?,?,?,?,?,'REVIEW_REWORK',?,?)",
                    (
                        rework_task_id,
                        record.id,
                        str(project_id),
                        str(milestone_id),
                        pr_id,
                        json.dumps(
                            task.to_dict(), sort_keys=True, separators=(",", ":")
                        ),
                        completed.isoformat(),
                    ),
                )
                changed = self.milestones.apply_rework_transition(
                    MilestoneTransitionRequest(
                        milestone_id,
                        project_id,
                        MilestoneState.ARCHITECT_REVIEW,
                        MilestoneState.REVIEW_REWORK,
                        "Architect requested implementation changes",
                        "SYSTEM",
                        "architect-review",
                        correlation_id,
                        completed,
                        metadata={"review_id": record.id, "pull_request_id": pr_id},
                    ),
                    "architect",
                    self.limit,
                    exhaustion_reason="Architect review rework limit exhausted",
                )
                if changed.state is MilestoneState.REVIEW_REWORK:
                    self.rework.enqueue(
                        project_id,
                        milestone_id,
                        record.id,
                        rework_task_id,
                        pr_id,
                        correlation_id,
                        completed,
                    )
            # M25/M26 verdicts are durably represented but deliberately cause no transition.
        return record

    def _build_request(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        correlation_id: str,
        expected_head_sha: str | None,
    ) -> tuple[ArchitectReviewRequest, str, str, PullRequestRecord]:
        milestone = self.milestones.get(milestone_id, project_id)
        if milestone.state is not MilestoneState.ARCHITECT_REVIEW:
            raise ArchitectReviewError("Architect review requires ARCHITECT_REVIEW")
        persisted = self.prs.for_milestone(milestone_id)
        if (
            persisted is None
            or persisted.project_id != project_id
            or persisted.state is not PullRequestState.OPEN
        ):
            raise ArchitectReviewError("one trusted open implementation PR is required")
        repo = self.connection.execute(
            "SELECT * FROM github_repositories WHERE id=? AND project_id=? AND status='VERIFIED'",
            (persisted.github_repository_id, str(project_id)),
        ).fetchone()
        if repo is None:
            raise ArchitectReviewError("verified repository identity is missing")
        live = self.github_prs.get(
            repo["full_name"], persisted.external_pr_number, project_id, milestone_id
        )
        self._verify_live(live, persisted, int(repo["external_repository_id"]))
        if live.head_sha != persisted.head_sha or (
            expected_head_sha is not None and live.head_sha != expected_head_sha
        ):
            raise ArchitectReviewError(
                "live PR head differs from expected persisted head"
            )
        ci = self.connection.execute(
            "SELECT * FROM ci_runs WHERE pull_request_id=? AND head_sha=? AND overall_status='PASSED' ORDER BY attempt_number DESC LIMIT 1",
            (persisted.id, live.head_sha),
        ).fetchone()
        if ci is None:
            raise ArchitectReviewError(
                "exact current PR head lacks passing CI evidence"
            )
        documents = self.connection.execute(
            "SELECT * FROM project_documents WHERE project_id=? AND status='APPROVED' AND document_type IN ('SPEC','AGENTS') ORDER BY revision DESC",
            (str(project_id),),
        ).fetchall()
        current: dict[str, sqlite3.Row] = {}
        for document in documents:
            current.setdefault(document["document_type"], document)
        if set(current) != {DocumentType.SPEC.value, DocumentType.AGENTS.value}:
            raise ArchitectReviewError(
                "approved SPEC and AGENTS revisions are required"
            )
        diff = self.review_context.diff(
            repo["full_name"], persisted.external_pr_number, live.head_sha
        )
        # A second read binds the returned diff to the same current head.
        after_diff = self.github_prs.get(
            repo["full_name"], persisted.external_pr_number, project_id, milestone_id
        )
        self._verify_live(after_diff, persisted, int(repo["external_repository_id"]))
        if after_diff.head_sha != live.head_sha:
            raise ArchitectReviewError("PR head changed while review context was built")
        prior = tuple(
            ReviewFinding(
                row["finding_code"],
                ReviewFindingSeverity(row["severity"]),
                row["requirement_ref"],
                row["description"],
                row["recommended_action"],
            )
            for row in self.reviews.previous_open_findings(
                str(project_id), str(milestone_id), persisted.id
            )
        )
        definition = {
            "code": milestone.code,
            "title": milestone.title,
            "requirements": self._json_column(milestone_id, "definition_json"),
            "acceptance_criteria": self._json_column(
                milestone_id, "automated_acceptance_json"
            ),
        }

        def source(row: sqlite3.Row) -> dict[str, object]:
            return {
                "revision": int(row["revision"]),
                "content": row["content"],
                "hash": row["content_hash"],
            }

        request = ArchitectReviewRequest(
            ARCHITECT_INTERFACE_VERSION,
            correlation_id,
            project_id,
            milestone_id,
            persisted.external_pr_number,
            live.head_sha,
            definition,
            source(current["SPEC"]),
            source(current["AGENTS"]),
            diff,
            {
                "run_id": ci["id"],
                "head_sha": ci["head_sha"],
                "status": ci["overall_status"],
                "external_workflow_run_id": ci["external_workflow_run_id"],
            },
            prior,
        )
        return request, persisted.id, repo["full_name"], persisted

    def _json_column(self, milestone_id: MilestoneId, name: str) -> object:
        columns = {"definition_json", "automated_acceptance_json"}
        if name not in columns:
            raise ValueError("unsupported milestone column")
        row = self.connection.execute(
            f"SELECT {name} FROM milestones WHERE id=?", (str(milestone_id),)
        ).fetchone()
        value = row[name] if row else None
        return json.loads(value) if value else []

    @staticmethod
    def _validate_response(
        request: ArchitectReviewRequest, response: ArchitectReview
    ) -> None:
        if (
            response.interface_version != request.interface_version
            or response.correlation_id != request.correlation_id
            or response.project_id != request.project_id
            or response.milestone_id != request.milestone_id
            or response.pull_request_number != request.pull_request_number
            or response.reviewed_sha != request.head_sha
        ):
            raise ArchitectReviewError(
                "Architect review identity does not match request"
            )

    @staticmethod
    def _verify_live(
        live: PullRequestDescriptor, persisted: object, repository_id: int
    ) -> None:
        if (
            not isinstance(persisted, PullRequestRecord)
            or live.repository_id != repository_id
            or live.pull_request_number != persisted.external_pr_number
            or live.head_branch != persisted.head_branch
            or live.base_branch != persisted.base_branch
            or live.state is not PullRequestState.OPEN
        ):
            raise ArchitectReviewError(
                "live pull request identity differs from persisted ownership"
            )

    def _repository_external_id(self, repository_id: str) -> int:
        row = self.connection.execute(
            "SELECT external_repository_id FROM github_repositories WHERE id=?",
            (repository_id,),
        ).fetchone()
        if row is None:
            raise ArchitectReviewError("repository identity is missing")
        return int(row[0])


class ArchitectApprovalFreshness:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def is_current(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        pull_request_id: str,
        current_head_sha: str,
    ) -> bool:
        current_pr = self.connection.execute(
            """SELECT 1 FROM pull_requests WHERE id=? AND project_id=?
            AND milestone_id=? AND state='OPEN' AND head_sha=?""",
            (
                pull_request_id,
                str(project_id),
                str(milestone_id),
                current_head_sha,
            ),
        ).fetchone()
        if current_pr is None:
            return False
        row = self.connection.execute(
            """SELECT r.verdict,r.reviewed_sha,r.superseded_at FROM architect_reviews r WHERE r.project_id=? AND r.milestone_id=? AND r.pull_request_id=? ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1""",
            (str(project_id), str(milestone_id), pull_request_id),
        ).fetchone()
        if (
            row is None
            or row["verdict"] != "APPROVE"
            or row["reviewed_sha"] != current_head_sha
            or row["superseded_at"] is not None
        ):
            return False
        ci = self.connection.execute(
            "SELECT 1 FROM ci_runs WHERE pull_request_id=? AND head_sha=? AND overall_status='PASSED' LIMIT 1",
            (pull_request_id, current_head_sha),
        ).fetchone()
        blocking = self.connection.execute(
            """SELECT 1 FROM architect_review_findings f JOIN architect_reviews r ON r.id=f.review_id WHERE r.project_id=? AND r.milestone_id=? AND r.pull_request_id=? AND f.status='OPEN' LIMIT 1""",
            (str(project_id), str(milestone_id), pull_request_id),
        ).fetchone()
        return ci is not None and blocking is None
