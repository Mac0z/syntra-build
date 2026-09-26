# ruff: noqa: E501
"""M26 deterministic Gatekeeper, durable merge intent, and verified completion."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from syntra_build.application.architect_review import ArchitectApprovalFreshness
from syntra_build.application.human_intervention import HumanTestFreshness
from syntra_build.application.provisioning import AmbiguousGitHubResult
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.merges import (
    MERGE_INTERFACE_VERSION,
    MergeEligibilityRequest,
    MergeEligibilityResult,
    MergeGuardResult,
    MergeRequest,
    MergeResult,
    MergeStatus,
    MergeStrategy,
)
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)


class GatekeeperRejected(RuntimeError):
    def __init__(self, result: MergeEligibilityResult) -> None:
        self.result = result
        super().__init__("deterministic merge policy rejected the request")


class MergeBusy(RuntimeError):
    """Another globally serialized merge has already persisted its intent."""


class GitHubMergeGateway(Protocol):
    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor: ...
    def merge(
        self, repository_full_name: str, request: MergeRequest
    ) -> MergeResult: ...


class Gatekeeper:
    """Evaluates policy solely from persisted evidence and a trusted live PR GET."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        github: GitHubMergeGateway,
        *,
        id_factory: Callable[[], str] | None = None,
        security_blocked: Callable[[ProjectId, MilestoneId], bool] | None = None,
    ) -> None:
        self.connection = connection
        self.github = github
        self.id_factory = id_factory or (lambda: str(uuid4()))
        self.security_blocked = security_blocked or (lambda _project, _milestone: False)

    def evaluate(
        self,
        request: MergeEligibilityRequest,
        *,
        now: datetime | None = None,
        persist: bool = True,
    ) -> MergeEligibilityResult:
        now = now or datetime.now(UTC)
        repo = self.connection.execute(
            "SELECT * FROM github_repositories WHERE id=?",
            (request.github_repository_id,),
        ).fetchone()
        full_name = repo["full_name"] if repo is not None else "invalid/identity"
        try:
            live = self.github.get(
                full_name,
                request.pull_request_number,
                request.project_id,
                request.milestone_id,
            )
        except Exception:
            live = None
        result = self._evaluate_persisted(request, live, now)
        if persist:
            with transaction(self.connection):
                self._persist_result(request, result)
        return result

    def _evaluate_persisted(
        self,
        request: MergeEligibilityRequest,
        live: PullRequestDescriptor | None,
        now: datetime,
    ) -> MergeEligibilityResult:
        db = self.connection
        project = db.execute(
            "SELECT * FROM projects WHERE id=?", (str(request.project_id),)
        ).fetchone()
        milestone = db.execute(
            "SELECT * FROM milestones WHERE id=?", (str(request.milestone_id),)
        ).fetchone()
        repo = db.execute(
            "SELECT * FROM github_repositories WHERE id=?",
            (request.github_repository_id,),
        ).fetchone()
        pr = db.execute(
            "SELECT * FROM pull_requests WHERE id=?", (request.pull_request_id,)
        ).fetchone()
        guards: list[MergeGuardResult] = []

        def guard(name: str, condition: bool, failure: str) -> None:
            guards.append(
                MergeGuardResult(name, bool(condition), "ok" if condition else failure)
            )

        guard("project_identity", project is not None, "project_not_found")
        guard(
            "project_state",
            project is not None and project["state"] == "BUILDING",
            "project_state_incompatible",
        )
        guard(
            "milestone_identity",
            milestone is not None
            and milestone["project_id"] == str(request.project_id),
            "milestone_project_mismatch",
        )
        guard(
            "milestone_state",
            milestone is not None and milestone["state"] == "MERGE_READY",
            "milestone_not_merge_ready",
        )
        guard(
            "repository_identity",
            repo is not None
            and repo["project_id"] == str(request.project_id)
            and repo["external_repository_id"] == request.repository_id,
            "repository_identity_mismatch",
        )
        guard(
            "repository_verified",
            repo is not None and repo["status"] == "VERIFIED",
            "repository_not_verified",
        )
        owned_pr = (
            pr is not None
            and pr["project_id"] == str(request.project_id)
            and pr["milestone_id"] == str(request.milestone_id)
            and pr["github_repository_id"] == request.github_repository_id
        )
        guard(
            "pull_request_identity",
            owned_pr and pr["external_pr_number"] == request.pull_request_number,
            "pull_request_identity_mismatch",
        )
        guard(
            "persisted_branches",
            owned_pr
            and pr["head_branch"] == request.expected_head_branch
            and pr["base_branch"] == request.expected_base_branch,
            "persisted_branch_mismatch",
        )
        guard(
            "persisted_head",
            owned_pr and pr["head_sha"] == request.expected_head_sha,
            "persisted_head_mismatch",
        )
        guard("live_observation", live is not None, "live_pull_request_unavailable")
        guard(
            "live_repository",
            live is not None and live.repository_id == request.repository_id,
            "live_repository_mismatch",
        )
        guard(
            "live_pull_request",
            live is not None
            and live.pull_request_number == request.pull_request_number,
            "live_pull_request_mismatch",
        )
        guard(
            "live_open",
            live is not None and live.state is PullRequestState.OPEN,
            "pull_request_not_open",
        )
        guard(
            "live_branches",
            live is not None
            and live.head_branch == request.expected_head_branch
            and live.base_branch == request.expected_base_branch,
            "live_branch_mismatch",
        )
        guard(
            "live_head",
            live is not None and live.head_sha == request.expected_head_sha,
            "live_head_mismatch",
        )
        ci_current = (
            db.execute(
                "SELECT 1 FROM ci_runs WHERE pull_request_id=? AND head_sha=? AND overall_status='PASSED' LIMIT 1",
                (request.pull_request_id, request.expected_head_sha),
            ).fetchone()
            is not None
        )
        guard("required_ci", ci_current, "current_ci_not_passed")
        architect = ArchitectApprovalFreshness(db).is_current(
            request.project_id,
            request.milestone_id,
            request.pull_request_id,
            request.expected_head_sha,
        )
        guard("architect_approval", architect, "current_architect_approval_missing")
        findings = db.execute(
            """SELECT 1 FROM architect_review_findings f JOIN architect_reviews r ON r.id=f.review_id WHERE r.project_id=? AND r.milestone_id=? AND r.pull_request_id=? AND f.status='OPEN' LIMIT 1""",
            (
                str(request.project_id),
                str(request.milestone_id),
                request.pull_request_id,
            ),
        ).fetchone()
        guard("blocking_findings", findings is None, "blocking_finding_open")
        unresolved = db.execute(
            "SELECT 1 FROM human_gates WHERE project_id=? AND milestone_id=? AND state NOT IN ('RESOLVED','CANCELLED','EXPIRED') LIMIT 1",
            (str(request.project_id), str(request.milestone_id)),
        ).fetchone()
        guard("human_gates", unresolved is None, "human_gate_unresolved")
        human_required = (
            db.execute(
                "SELECT 1 FROM human_gates WHERE project_id=? AND milestone_id=? AND gate_type='HUMAN_TEST' LIMIT 1",
                (str(request.project_id), str(request.milestone_id)),
            ).fetchone()
            is not None
        )
        human_current = HumanTestFreshness(db).is_current(
            request.project_id,
            request.milestone_id,
            request.pull_request_id,
            request.expected_head_sha,
        )
        guard(
            "human_test",
            not human_required or human_current,
            "current_human_test_pass_missing",
        )
        guard(
            "security_policy",
            not self.security_blocked(request.project_id, request.milestone_id),
            "security_policy_block",
        )
        return MergeEligibilityResult(
            self.id_factory(),
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.repository_id,
            request.pull_request_number,
            request.expected_head_sha,
            all(item.passed for item in guards),
            tuple(guards),
            now,
        )

    def _persist_result(
        self, request: MergeEligibilityRequest, result: MergeEligibilityResult
    ) -> None:
        self.connection.execute(
            """INSERT INTO merge_eligibility_results
            (id,correlation_id,project_id,milestone_id,pull_request_id,repository_id,
             pull_request_number,head_sha,eligible,guards_json,evaluated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result.result_id,
                result.correlation_id,
                str(result.project_id),
                str(result.milestone_id),
                request.pull_request_id,
                result.repository_id,
                result.pull_request_number,
                result.head_sha,
                int(result.eligible),
                json.dumps([asdict(item) for item in result.guards], sort_keys=True),
                result.evaluated_at.isoformat(),
            ),
        )

    def prepare(
        self,
        request: MergeEligibilityRequest,
        strategy: MergeStrategy = MergeStrategy.SQUASH,
        *,
        now: datetime | None = None,
    ) -> tuple[str, MergeRequest]:
        """Atomically persist approval, intent, and MERGING before any mutation."""
        now = now or datetime.now(UTC)
        repo = self.connection.execute(
            "SELECT full_name FROM github_repositories WHERE id=?",
            (request.github_repository_id,),
        ).fetchone()
        live = self.github.get(
            repo["full_name"] if repo else "invalid/identity",
            request.pull_request_number,
            request.project_id,
            request.milestone_id,
        )
        result = self._evaluate_persisted(request, live, now)
        if not result.eligible:
            with transaction(self.connection):
                self._persist_result(request, result)
            raise GatekeeperRejected(result)
        with transaction(self.connection):
            # Re-evaluate persisted state while holding the write transaction.  The
            # already-fetched live observation is the immediately preceding trusted GET.
            result = self._evaluate_persisted(request, live, now)
            self._persist_result(request, result)
            if not result.eligible:
                raise GatekeeperRejected(result)
            attempt_id = self.id_factory()
            result_json = json.dumps(
                {
                    "result_id": result.result_id,
                    "eligible": result.eligible,
                    "guards": [asdict(item) for item in result.guards],
                },
                sort_keys=True,
            )
            try:
                self.connection.execute(
                    """INSERT INTO merge_attempts
                    (id,project_id,milestone_id,pull_request_id,expected_head_sha,
                     gatekeeper_result_id,gatekeeper_result_json,merge_strategy,status,requested_at)
                    VALUES (?,?,?,?,?,?,?,?, 'REQUESTED',?)""",
                    (
                        attempt_id,
                        str(request.project_id),
                        str(request.milestone_id),
                        request.pull_request_id,
                        request.expected_head_sha,
                        result.result_id,
                        result_json,
                        strategy.value,
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                if "one_global_active_merge" in str(
                    error
                ) or "UNIQUE constraint" in str(error):
                    raise MergeBusy("another merge is active") from error
                raise
            SQLiteMilestoneRepository(
                self.connection, self.id_factory
            ).apply_transition(
                MilestoneTransitionRequest(
                    request.milestone_id,
                    request.project_id,
                    MilestoneState.MERGE_READY,
                    MilestoneState.MERGING,
                    "Gatekeeper approved durable merge intent",
                    "SYSTEM",
                    "gatekeeper",
                    request.correlation_id,
                    now,
                    metadata={
                        "merge_attempt_id": attempt_id,
                        "gatekeeper_result_id": result.result_id,
                    },
                )
            )
        return attempt_id, MergeRequest(
            MERGE_INTERFACE_VERSION,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.repository_id,
            request.pull_request_number,
            request.expected_head_sha,
            strategy,
            result.result_id,
        )

    def execute(
        self,
        attempt_id: str,
        request: MergeRequest,
        repository_full_name: str,
        *,
        now: datetime | None = None,
    ) -> MergeResult:
        """Execute once; ambiguity is reconciled by GET and never replayed."""
        now = now or datetime.now(UTC)
        row = self.connection.execute(
            """SELECT a.* FROM merge_attempts a
            JOIN milestones m ON m.id=a.milestone_id
            WHERE a.id=? AND a.status='REQUESTED' AND m.state='MERGING'""",
            (attempt_id,),
        ).fetchone()
        if (
            row is None
            or row["expected_head_sha"] != request.expected_head_sha
            or row["gatekeeper_result_id"] != request.gatekeeper_result_id
        ):
            raise RuntimeError("merge attempt is not executable")
        try:
            result = self.github.merge(repository_full_name, request)
        except AmbiguousGitHubResult:
            result = self._reconcile_ambiguous(repository_full_name, request)
        if result.status is MergeStatus.UNKNOWN:
            result = self._reconcile_ambiguous(repository_full_name, request)
        with transaction(self.connection):
            if result.status is MergeStatus.MERGED:
                SQLiteMilestoneRepository(
                    self.connection, self.id_factory
                ).apply_transition(
                    MilestoneTransitionRequest(
                        request.milestone_id,
                        request.project_id,
                        MilestoneState.MERGING,
                        MilestoneState.MERGE_VERIFY,
                        "provider merge requires independent verification",
                        "SYSTEM",
                        "gatekeeper",
                        request.correlation_id,
                        now,
                        metadata={"merge_attempt_id": attempt_id},
                    )
                )
            else:
                self.connection.execute(
                    "UPDATE merge_attempts SET status=?,completed_at=?,error_detail=? WHERE id=? AND status='REQUESTED'",
                    (result.status.value, now.isoformat(), result.detail, attempt_id),
                )
        return result

    def _reconcile_ambiguous(
        self, repository_full_name: str, request: MergeRequest
    ) -> MergeResult:
        try:
            observed = self.github.get(
                repository_full_name,
                request.pull_request_number,
                request.project_id,
                request.milestone_id,
            )
            if observed.state is PullRequestState.MERGED:
                result = MergeResult(
                    MERGE_INTERFACE_VERSION,
                    request.project_id,
                    request.milestone_id,
                    request.pull_request_number,
                    MergeStatus.MERGED,
                    observed.merge_commit_sha,
                    datetime.fromisoformat(observed.merged_at.replace("Z", "+00:00"))
                    if observed.merged_at
                    else None,
                )
                return result
        except Exception:
            pass
        return MergeResult(
            MERGE_INTERFACE_VERSION,
            request.project_id,
            request.milestone_id,
            request.pull_request_number,
            MergeStatus.UNKNOWN,
            detail="mutation outcome remains ambiguous",
        )

    def verify(
        self,
        attempt_id: str,
        request: MergeRequest,
        repository_full_name: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Use a separate trusted GET before persisting MERGED and COMPLETE."""
        now = now or datetime.now(UTC)
        observed = self.github.get(
            repository_full_name,
            request.pull_request_number,
            request.project_id,
            request.milestone_id,
        )
        exact = (
            observed.repository_id == request.repository_id
            and observed.pull_request_number == request.pull_request_number
            and observed.head_sha == request.expected_head_sha
            and observed.state is PullRequestState.MERGED
            and bool(observed.merge_commit_sha)
            and bool(observed.merged_at)
        )
        if not exact:
            return False
        with transaction(self.connection):
            attempt = self.connection.execute(
                "SELECT * FROM merge_attempts WHERE id=? AND status='REQUESTED'",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                return False
            pr = SQLitePullRequestRepository(self.connection).for_milestone(
                request.milestone_id
            )
            if pr is None:
                return False
            SQLitePullRequestRepository(self.connection).save_verified(
                pr.id, pr.github_repository_id, observed, pr.title, now
            )
            self.connection.execute(
                "UPDATE merge_attempts SET status='MERGED',completed_at=?,merge_commit_sha=? WHERE id=? AND status='REQUESTED'",
                (now.isoformat(), observed.merge_commit_sha, attempt_id),
            )
            SQLiteMilestoneRepository(
                self.connection, self.id_factory
            ).apply_transition(
                MilestoneTransitionRequest(
                    request.milestone_id,
                    request.project_id,
                    MilestoneState.MERGE_VERIFY,
                    MilestoneState.COMPLETE,
                    "independent GitHub observation verified merge",
                    "SYSTEM",
                    "gatekeeper",
                    request.correlation_id,
                    now,
                    metadata={
                        "merge_attempt_id": attempt_id,
                        "merge_commit_sha": observed.merge_commit_sha,
                    },
                )
            )
        return True
