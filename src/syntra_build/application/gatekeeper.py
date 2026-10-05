# ruff: noqa: E501
"""M26 deterministic Gatekeeper, durable merge intent, and verified completion."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from syntra_build.application.architect_review import ArchitectApprovalFreshness
from syntra_build.application.human_intervention import HumanTestFreshness
from syntra_build.application.provisioning import AmbiguousGitHubResult
from syntra_build.application.security import SecurityPolicy
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.merges import (
    MERGE_INTERFACE_VERSION,
    MergeEligibilityRequest,
    MergeEligibilityResult,
    MergeEvidence,
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


class MergeIdentityError(RuntimeError):
    """A merge request is not bound to its exact durable attempt."""


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


@dataclass(frozen=True, slots=True)
class _AttemptContext:
    attempt_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    pull_request_id: str
    github_repository_id: str
    repository_id: int
    repository_full_name: str
    pull_request_number: int
    head_branch: str
    base_branch: str
    expected_head_sha: str
    gatekeeper_result_id: str
    merge_strategy: MergeStrategy
    status: MergeStatus | None
    mutation_started_at: str | None
    milestone_state: MilestoneState
    project_state: str
    persisted_pr_state: PullRequestState
    persisted_head_sha: str
    persisted_head_branch: str
    persisted_base_branch: str


class Gatekeeper:
    """Evaluate and execute merge policy without AI authority or caller target selection."""

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
        self.security_blocked = security_blocked or SecurityPolicy(connection).blocked

    def evaluate(
        self,
        request: MergeEligibilityRequest,
        *,
        now: datetime | None = None,
        persist: bool = True,
    ) -> MergeEligibilityResult:
        now = now or datetime.now(UTC)
        repo = self.connection.execute(
            "SELECT full_name FROM github_repositories WHERE id=?",
            (request.github_repository_id,),
        ).fetchone()
        live: PullRequestDescriptor | None = None
        if repo is not None:
            try:
                live = self.github.get(
                    repo["full_name"],
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

        ci = db.execute(
            """SELECT id FROM ci_runs WHERE pull_request_id=? AND head_sha=?
            AND overall_status='PASSED' ORDER BY completed_at DESC,rowid DESC LIMIT 1""",
            (request.pull_request_id, request.expected_head_sha),
        ).fetchone()
        guard("required_ci", ci is not None, "current_ci_not_passed")
        review = db.execute(
            """SELECT id,verdict,reviewed_sha,superseded_at FROM architect_reviews
            WHERE project_id=? AND milestone_id=? AND pull_request_id=?
            ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (
                str(request.project_id),
                str(request.milestone_id),
                request.pull_request_id,
            ),
        ).fetchone()
        architect = ArchitectApprovalFreshness(db).is_current(
            request.project_id,
            request.milestone_id,
            request.pull_request_id,
            request.expected_head_sha,
        )
        guard("architect_approval", architect, "current_architect_approval_missing")
        finding = db.execute(
            """SELECT f.id FROM architect_review_findings f
            JOIN architect_reviews r ON r.id=f.review_id WHERE r.project_id=?
            AND r.milestone_id=? AND r.pull_request_id=? AND f.status='OPEN' LIMIT 1""",
            (
                str(request.project_id),
                str(request.milestone_id),
                request.pull_request_id,
            ),
        ).fetchone()
        guard("blocking_findings", finding is None, "blocking_finding_open")

        gates = db.execute(
            """SELECT id,gate_type,state FROM human_gates
            WHERE project_id=? AND milestone_id=? ORDER BY created_at,id""",
            (str(request.project_id), str(request.milestone_id)),
        ).fetchall()
        responses = db.execute(
            """SELECT r.id,r.gate_id FROM human_gate_responses r
            JOIN human_gates g ON g.id=r.gate_id WHERE g.project_id=? AND g.milestone_id=?
            AND g.state='RESOLVED' ORDER BY r.responded_at,r.id""",
            (str(request.project_id), str(request.milestone_id)),
        ).fetchall()
        response_gate_ids = {row["gate_id"] for row in responses}
        guard(
            "human_gates",
            all(
                gate["state"] == "RESOLVED" and gate["id"] in response_gate_ids
                for gate in gates
            ),
            "human_gate_not_explicitly_resolved",
        )
        human_required = any(g["gate_type"] == "HUMAN_TEST" for g in gates)
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
        test = db.execute(
            """SELECT r.id,r.gate_id,r.ci_run_id FROM human_test_results r
            JOIN human_test_bindings b ON b.gate_id=r.gate_id
            WHERE b.project_id=? AND b.milestone_id=? AND b.pull_request_id=?
            AND r.outcome='PASS' AND r.tested_head_sha=? ORDER BY r.recorded_at DESC LIMIT 1""",
            (
                str(request.project_id),
                str(request.milestone_id),
                request.pull_request_id,
                request.expected_head_sha,
            ),
        ).fetchone()
        guard(
            "security_policy",
            not self.security_blocked(request.project_id, request.milestone_id),
            "security_policy_block",
        )

        evidence = MergeEvidence(
            request.github_repository_id if repo is not None else None,
            request.pull_request_id if pr is not None else None,
            ci["id"] if ci else None,
            review["id"] if review else None,
            tuple(g["id"] for g in gates),
            tuple(r["id"] for r in responses),
            test["gate_id"] if test else None,
            test["id"] if test else None,
            test["ci_run_id"] if test else None,
        )
        return MergeEligibilityResult(
            MERGE_INTERFACE_VERSION,
            self.id_factory(),
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.repository_id,
            request.pull_request_number,
            request.expected_head_sha,
            all(item.passed for item in guards),
            tuple(guards),
            evidence,
            now,
        )

    def _persist_result(
        self, request: MergeEligibilityRequest, result: MergeEligibilityResult
    ) -> None:
        self.connection.execute(
            """INSERT INTO merge_eligibility_results
            (id,interface_version,correlation_id,project_id,milestone_id,
             github_repository_id,pull_request_id,repository_id,pull_request_number,
             head_sha,eligible,guards_json,evidence_json,evaluated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result.result_id,
                result.interface_version,
                result.correlation_id,
                str(result.project_id),
                str(result.milestone_id),
                request.github_repository_id,
                request.pull_request_id,
                result.repository_id,
                result.pull_request_number,
                result.head_sha,
                int(result.eligible),
                json.dumps([asdict(item) for item in result.guards], sort_keys=True),
                json.dumps(asdict(result.evidence), sort_keys=True),
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
        result = self.evaluate(request, now=now, persist=False)
        if not result.eligible:
            with transaction(self.connection):
                self._persist_result(request, result)
            raise GatekeeperRejected(result)
        # Provider observation must complete before BEGIN IMMEDIATE acquires the
        # process-wide SQLite writer reservation. Persisted policy is evaluated
        # again inside the transaction so this observation cannot authorize a
        # merge after authoritative state changes concurrently.
        live = self._fresh_pr(request)
        with transaction(self.connection):
            result = self._evaluate_persisted(request, live, now)
            self._persist_result(request, result)
            if not result.eligible:
                raise GatekeeperRejected(result)
            attempt_id = self.id_factory()
            result_json = json.dumps(
                {
                    "interface_version": result.interface_version,
                    "result_id": result.result_id,
                    "eligible": result.eligible,
                    "guards": [asdict(item) for item in result.guards],
                    "evidence": asdict(result.evidence),
                },
                sort_keys=True,
            )
            try:
                self.connection.execute(
                    """INSERT INTO merge_attempts
                    (id,project_id,milestone_id,pull_request_id,expected_head_sha,
                     expected_head_branch,expected_base_branch,
                     gatekeeper_result_id,gatekeeper_result_json,merge_strategy,status,requested_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?, 'REQUESTED',?)""",
                    (
                        attempt_id,
                        str(request.project_id),
                        str(request.milestone_id),
                        request.pull_request_id,
                        request.expected_head_sha,
                        request.expected_head_branch,
                        request.expected_base_branch,
                        result.result_id,
                        result_json,
                        strategy.value,
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                if "UNIQUE constraint" in str(error):
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

    def _fresh_pr(self, request: MergeEligibilityRequest) -> PullRequestDescriptor:
        repo = self.connection.execute(
            "SELECT full_name FROM github_repositories WHERE id=?",
            (request.github_repository_id,),
        ).fetchone()
        if repo is None:
            raise MergeIdentityError("persisted repository does not exist")
        return self.github.get(
            repo["full_name"],
            request.pull_request_number,
            request.project_id,
            request.milestone_id,
        )

    def _attempt_context(
        self, attempt_id: str, request: MergeRequest
    ) -> _AttemptContext:
        row = self.connection.execute(
            """SELECT a.*,m.state AS milestone_state,project.state AS project_state,
            p.github_repository_id,p.external_pr_number,p.state AS persisted_pr_state,
            p.head_sha AS persisted_head_sha,p.head_branch AS persisted_head_branch,
            p.base_branch AS persisted_base_branch,
            r.external_repository_id,r.full_name,r.status AS repository_status
            FROM merge_attempts a JOIN milestones m ON m.id=a.milestone_id
            JOIN projects project ON project.id=a.project_id
            JOIN pull_requests p ON p.id=a.pull_request_id
            JOIN github_repositories r ON r.id=p.github_repository_id WHERE a.id=?""",
            (attempt_id,),
        ).fetchone()
        if row is None or row["repository_status"] != "VERIFIED":
            raise MergeIdentityError(
                "merge attempt or verified repository is unavailable"
            )
        expected = (
            row["project_id"] == str(request.project_id),
            row["milestone_id"] == str(request.milestone_id),
            row["external_repository_id"] == request.repository_id,
            row["external_pr_number"] == request.pull_request_number,
            row["expected_head_sha"] == request.expected_head_sha,
            row["gatekeeper_result_id"] == request.gatekeeper_result_id,
            row["merge_strategy"] == request.merge_strategy.value,
        )
        if not all(expected):
            raise MergeIdentityError(
                "merge request identity differs from durable attempt"
            )
        return _AttemptContext(
            row["id"],
            ProjectId.from_string(row["project_id"]),
            MilestoneId.from_string(row["milestone_id"]),
            row["pull_request_id"],
            row["github_repository_id"],
            row["external_repository_id"],
            row["full_name"],
            row["external_pr_number"],
            row["expected_head_branch"],
            row["expected_base_branch"],
            row["expected_head_sha"],
            row["gatekeeper_result_id"],
            MergeStrategy(row["merge_strategy"]),
            None if row["status"] == "REQUESTED" else MergeStatus(row["status"]),
            row["mutation_started_at"],
            MilestoneState(row["milestone_state"]),
            row["project_state"],
            PullRequestState(row["persisted_pr_state"]),
            row["persisted_head_sha"],
            row["persisted_head_branch"],
            row["persisted_base_branch"],
        )

    @staticmethod
    def _live_matches(
        context: _AttemptContext, live: PullRequestDescriptor, *, require_merged: bool
    ) -> bool:
        expected_state = (
            PullRequestState.MERGED if require_merged else PullRequestState.OPEN
        )
        return (
            live.repository_id == context.repository_id
            and live.pull_request_number == context.pull_request_number
            and live.state is expected_state
            and live.head_branch == context.head_branch
            and live.base_branch == context.base_branch
            and live.head_sha == context.expected_head_sha
        )

    def _execution_policy_failure(self, context: _AttemptContext) -> str | None:
        """Revalidate mutable persisted merge authority after prepare committed."""
        if context.project_state != "BUILDING":
            return f"project state {context.project_state} does not permit merge"
        if context.persisted_pr_state is not PullRequestState.OPEN:
            return "persisted pull request is no longer open"
        if context.persisted_head_sha != context.expected_head_sha:
            return "persisted pull request head changed after merge preparation"
        if (
            context.persisted_head_branch != context.head_branch
            or context.persisted_base_branch != context.base_branch
        ):
            return "persisted pull request branches changed after merge preparation"
        if self.security_blocked(context.project_id, context.milestone_id):
            return "security policy blocks merge"
        ci = self.connection.execute(
            """SELECT 1 FROM ci_runs WHERE pull_request_id=? AND head_sha=?
            AND overall_status='PASSED' LIMIT 1""",
            (context.pull_request_id, context.expected_head_sha),
        ).fetchone()
        if ci is None:
            return "current required CI evidence is unavailable"
        if not ArchitectApprovalFreshness(self.connection).is_current(
            context.project_id,
            context.milestone_id,
            context.pull_request_id,
            context.expected_head_sha,
        ):
            return "current Architect approval is unavailable"
        finding = self.connection.execute(
            """SELECT 1 FROM architect_review_findings f
            JOIN architect_reviews r ON r.id=f.review_id
            WHERE r.project_id=? AND r.milestone_id=? AND r.pull_request_id=?
            AND f.status='OPEN' LIMIT 1""",
            (
                str(context.project_id),
                str(context.milestone_id),
                context.pull_request_id,
            ),
        ).fetchone()
        if finding is not None:
            return "a blocking Architect finding is open"
        gates = self.connection.execute(
            """SELECT g.id,g.gate_type,g.state,
            EXISTS(SELECT 1 FROM human_gate_responses response
              WHERE response.gate_id=g.id AND response.validated=1) AS has_response
            FROM human_gates g WHERE g.project_id=? AND g.milestone_id=?""",
            (str(context.project_id), str(context.milestone_id)),
        ).fetchall()
        if any(
            gate["state"] != "RESOLVED" or not bool(gate["has_response"])
            for gate in gates
        ):
            return "a human gate lacks explicit valid resolution"
        if any(gate["gate_type"] == "HUMAN_TEST" for gate in gates) and not (
            HumanTestFreshness(self.connection).is_current(
                context.project_id,
                context.milestone_id,
                context.pull_request_id,
                context.expected_head_sha,
            )
        ):
            return "current human test PASS evidence is unavailable"
        return None

    def _terminalize_before_put(
        self,
        context: _AttemptContext,
        request: MergeRequest,
        now: datetime,
        reason: str,
    ) -> MergeResult:
        """Reject deterministically, release the global slot, and block the milestone."""
        with transaction(self.connection):
            self.connection.execute(
                """UPDATE merge_attempts SET status='REJECTED',completed_at=?,
                error_detail=? WHERE id=? AND status='REQUESTED'""",
                (now.isoformat(), reason, context.attempt_id),
            )
            SQLiteMilestoneRepository(
                self.connection, self.id_factory
            ).apply_transition(
                MilestoneTransitionRequest(
                    context.milestone_id,
                    context.project_id,
                    MilestoneState.MERGING,
                    MilestoneState.BLOCKED,
                    reason,
                    "SYSTEM",
                    "gatekeeper",
                    request.correlation_id,
                    now,
                    metadata={
                        "merge_attempt_id": context.attempt_id,
                        "merge_status": MergeStatus.REJECTED.value,
                        "phase": "PRE_PUT_REVALIDATION",
                    },
                )
            )
        return MergeResult(
            MERGE_INTERFACE_VERSION,
            context.project_id,
            context.milestone_id,
            context.pull_request_number,
            MergeStatus.REJECTED,
            detail=reason,
        )

    def _advance_observed_merge(
        self,
        context: _AttemptContext,
        request: MergeRequest,
        now: datetime,
        observed: PullRequestDescriptor,
    ) -> MergeResult:
        """Adopt an exact already-merged observation without replaying the PUT."""
        with transaction(self.connection):
            SQLiteMilestoneRepository(
                self.connection, self.id_factory
            ).apply_transition(
                MilestoneTransitionRequest(
                    context.milestone_id,
                    context.project_id,
                    MilestoneState.MERGING,
                    MilestoneState.MERGE_VERIFY,
                    "fresh GitHub observation found the exact PR already merged",
                    "SYSTEM",
                    "gatekeeper",
                    request.correlation_id,
                    now,
                    metadata={
                        "merge_attempt_id": context.attempt_id,
                        "reconciled_without_put": True,
                    },
                )
            )
        return MergeResult(
            MERGE_INTERFACE_VERSION,
            context.project_id,
            context.milestone_id,
            context.pull_request_number,
            MergeStatus.MERGED,
            observed.merge_commit_sha,
            datetime.fromisoformat(observed.merged_at.replace("Z", "+00:00"))
            if observed.merged_at
            else None,
        )

    def execute(
        self, attempt_id: str, request: MergeRequest, *, now: datetime | None = None
    ) -> MergeResult:
        """Freshly validate the persisted target, then execute the PUT at most once."""
        now = now or datetime.now(UTC)
        context = self._attempt_context(attempt_id, request)
        if (
            context.status is not None
            or context.mutation_started_at is not None
            or context.milestone_state is not MilestoneState.MERGING
        ):
            raise MergeIdentityError("merge attempt is not executable")
        policy_failure = self._execution_policy_failure(context)
        if policy_failure is not None:
            return self._terminalize_before_put(context, request, now, policy_failure)
        # A failed observation is intentionally propagated with REQUESTED/MERGING
        # unchanged so the bounded scheduler/recovery path may safely retry the GET.
        live = self.github.get(
            context.repository_full_name,
            context.pull_request_number,
            context.project_id,
            context.milestone_id,
        )
        if self._live_matches(context, live, require_merged=True):
            return self._advance_observed_merge(context, request, now, live)
        if not self._live_matches(context, live, require_merged=False):
            return self._terminalize_before_put(
                context,
                request,
                now,
                "fresh pull request identity or state differs before merge",
            )
        # Commit a durable point-of-no-return before the provider mutation.  A
        # restarted worker can now prove whether executing a REQUESTED attempt is
        # safe or whether it must use observation-only recovery.
        with transaction(self.connection):
            changed = self.connection.execute(
                """UPDATE merge_attempts SET mutation_started_at=?
                WHERE id=? AND status='REQUESTED' AND mutation_started_at IS NULL""",
                (now.isoformat(), attempt_id),
            )
            if changed.rowcount != 1:
                raise MergeIdentityError("merge mutation may already have started")
        try:
            result = self.github.merge(context.repository_full_name, request)
        except AmbiguousGitHubResult:
            result = self._reconcile_ambiguous(context, request)
        else:
            if result.status is MergeStatus.UNKNOWN:
                result = self._reconcile_ambiguous(context, request)
        if (
            result.project_id != context.project_id
            or result.milestone_id != context.milestone_id
            or result.pull_request_number != context.pull_request_number
        ):
            result = MergeResult(
                MERGE_INTERFACE_VERSION,
                context.project_id,
                context.milestone_id,
                context.pull_request_number,
                MergeStatus.UNKNOWN,
                detail="provider result identity mismatch",
            )
        with transaction(self.connection):
            if result.status is MergeStatus.MERGED:
                SQLiteMilestoneRepository(
                    self.connection, self.id_factory
                ).apply_transition(
                    MilestoneTransitionRequest(
                        context.milestone_id,
                        context.project_id,
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
            elif result.status is MergeStatus.UNKNOWN:
                self.connection.execute(
                    """UPDATE merge_attempts SET status='UNKNOWN',
                    completed_at=?,error_detail=? WHERE id=? AND status='REQUESTED'""",
                    (now.isoformat(), result.detail, attempt_id),
                )
            else:
                self.connection.execute(
                    """UPDATE merge_attempts SET status=?,completed_at=?,
                    error_detail=? WHERE id=? AND status='REQUESTED'""",
                    (result.status.value, now.isoformat(), result.detail, attempt_id),
                )
                SQLiteMilestoneRepository(
                    self.connection, self.id_factory
                ).apply_transition(
                    MilestoneTransitionRequest(
                        context.milestone_id,
                        context.project_id,
                        MilestoneState.MERGING,
                        MilestoneState.BLOCKED,
                        f"trusted merge ended with {result.status.value}",
                        "SYSTEM",
                        "gatekeeper",
                        request.correlation_id,
                        now,
                        metadata={
                            "merge_attempt_id": attempt_id,
                            "merge_status": result.status.value,
                        },
                    )
                )
        return result

    def _reconcile_ambiguous(
        self, context: _AttemptContext, request: MergeRequest
    ) -> MergeResult:
        try:
            observed = self.github.get(
                context.repository_full_name,
                context.pull_request_number,
                context.project_id,
                context.milestone_id,
            )
            if self._live_matches(context, observed, require_merged=True):
                return MergeResult(
                    MERGE_INTERFACE_VERSION,
                    context.project_id,
                    context.milestone_id,
                    context.pull_request_number,
                    MergeStatus.MERGED,
                    observed.merge_commit_sha,
                    datetime.fromisoformat(observed.merged_at.replace("Z", "+00:00"))
                    if observed.merged_at
                    else None,
                )
        except Exception:
            pass
        return MergeResult(
            MERGE_INTERFACE_VERSION,
            context.project_id,
            context.milestone_id,
            context.pull_request_number,
            MergeStatus.UNKNOWN,
            detail="mutation outcome remains ambiguous",
        )

    def verify(
        self, attempt_id: str, request: MergeRequest, *, now: datetime | None = None
    ) -> bool:
        """Derive the target from persistence and require an independent merged GET."""
        now = now or datetime.now(UTC)
        context = self._attempt_context(attempt_id, request)
        if (
            context.status is not None
            or context.milestone_state is not MilestoneState.MERGE_VERIFY
        ):
            raise MergeIdentityError("merge attempt is not awaiting verification")
        observed = self.github.get(
            context.repository_full_name,
            context.pull_request_number,
            context.project_id,
            context.milestone_id,
        )
        if (
            not self._live_matches(context, observed, require_merged=True)
            or not observed.merge_commit_sha
            or not observed.merged_at
        ):
            return False
        with transaction(self.connection):
            pr = SQLitePullRequestRepository(self.connection).for_milestone(
                context.milestone_id
            )
            if pr is None or pr.id != context.pull_request_id:
                raise MergeIdentityError(
                    "persisted pull request differs from merge attempt"
                )
            SQLitePullRequestRepository(self.connection).save_verified(
                pr.id, context.github_repository_id, observed, pr.title, now
            )
            self.connection.execute(
                """UPDATE merge_attempts SET status='MERGED',
                completed_at=?,merge_commit_sha=? WHERE id=? AND status='REQUESTED'""",
                (now.isoformat(), observed.merge_commit_sha, attempt_id),
            )
            SQLiteMilestoneRepository(
                self.connection, self.id_factory
            ).apply_transition(
                MilestoneTransitionRequest(
                    context.milestone_id,
                    context.project_id,
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

    def recover(
        self, attempt_id: str, request: MergeRequest, *, now: datetime | None = None
    ) -> bool:
        """Observe an interrupted merge without ever issuing a second merge PUT."""
        now = now or datetime.now(UTC)
        context = self._attempt_context(attempt_id, request)
        if context.milestone_state not in {
            MilestoneState.MERGING,
            MilestoneState.MERGE_VERIFY,
        }:
            raise MergeIdentityError("merge attempt is not recoverable")
        if context.status not in {None, MergeStatus.UNKNOWN}:
            raise MergeIdentityError("merge attempt has a terminal result")
        observed = self.github.get(
            context.repository_full_name,
            context.pull_request_number,
            context.project_id,
            context.milestone_id,
        )
        if not self._live_matches(context, observed, require_merged=True):
            # OPEN or ambiguous is deliberately not replayed. The recovery
            # coordinator applies project-scoped blocking policy.
            return False
        with transaction(self.connection):
            if context.milestone_state is MilestoneState.MERGING:
                SQLiteMilestoneRepository(
                    self.connection, self.id_factory
                ).apply_transition(
                    MilestoneTransitionRequest(
                        context.milestone_id,
                        context.project_id,
                        MilestoneState.MERGING,
                        MilestoneState.MERGE_VERIFY,
                        "recovery observed exact pull request already merged",
                        "RECOVERY",
                        "gatekeeper",
                        request.correlation_id,
                        now,
                        metadata={
                            "merge_attempt_id": attempt_id,
                            "reconciled_without_put": True,
                        },
                    )
                )
        # A second GET in verify is the required independent observation.
        return self._verify_recovery(attempt_id, request, now)

    def _verify_recovery(
        self, attempt_id: str, request: MergeRequest, now: datetime
    ) -> bool:
        context = self._attempt_context(attempt_id, request)
        observed = self.github.get(
            context.repository_full_name,
            context.pull_request_number,
            context.project_id,
            context.milestone_id,
        )
        if (
            context.milestone_state is not MilestoneState.MERGE_VERIFY
            or context.status not in {None, MergeStatus.UNKNOWN}
            or not self._live_matches(context, observed, require_merged=True)
            or not observed.merge_commit_sha
            or not observed.merged_at
        ):
            return False
        with transaction(self.connection):
            pr = SQLitePullRequestRepository(self.connection).for_milestone(
                context.milestone_id
            )
            if pr is None or pr.id != context.pull_request_id:
                raise MergeIdentityError("persisted pull request differs from attempt")
            SQLitePullRequestRepository(self.connection).save_verified(
                pr.id, context.github_repository_id, observed, pr.title, now
            )
            self.connection.execute(
                """UPDATE merge_attempts SET status='MERGED',completed_at=?,
                   merge_commit_sha=? WHERE id=? AND status IN ('REQUESTED','UNKNOWN')""",
                (now.isoformat(), observed.merge_commit_sha, attempt_id),
            )
            SQLiteMilestoneRepository(
                self.connection, self.id_factory
            ).apply_transition(
                MilestoneTransitionRequest(
                    context.milestone_id,
                    context.project_id,
                    MilestoneState.MERGE_VERIFY,
                    MilestoneState.COMPLETE,
                    "independent recovery observation verified merge",
                    "RECOVERY",
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
