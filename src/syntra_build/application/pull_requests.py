# ruff: noqa: E501
"""M22 trusted commit, push, and create-or-reconcile PR orchestration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from syntra_build.application.provisioning import AmbiguousGitHubResult
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.pull_requests import (
    PULL_REQUEST_INTERFACE_VERSION,
    PullRequestCreateRequest,
    PullRequestDescriptor,
    PullRequestState,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.pull_requests import (
    PullRequestPersistenceConflict,
    PullRequestRecord,
    SQLitePullRequestRepository,
)


class PullRequestFailure(StrEnum):
    PRECONDITION = "PRECONDITION"
    REPOSITORY_IDENTITY_MISMATCH = "REPOSITORY_IDENTITY_MISMATCH"
    BRANCH_MISMATCH = "BRANCH_MISMATCH"
    HEAD_SHA_MISMATCH = "HEAD_SHA_MISMATCH"
    PR_IDENTITY_MISMATCH = "PR_IDENTITY_MISMATCH"
    PR_COLLISION = "PR_COLLISION"
    PR_CLOSED = "PR_CLOSED"
    PR_MERGED_UNEXPECTEDLY = "PR_MERGED_UNEXPECTEDLY"
    AUTHENTICATION = "AUTHENTICATION"
    PROVIDER_REJECTION = "PROVIDER_REJECTION"
    TRANSIENT = "TRANSIENT"
    AMBIGUOUS = "AMBIGUOUS"
    PERSISTENCE = "PERSISTENCE"


class PullRequestError(RuntimeError):
    def __init__(self, failure: PullRequestFailure, message: str) -> None:
        self.failure = failure
        super().__init__(message)


class PullRequestCreateConflict(RuntimeError):
    """Creation may conflict with an already-existing provider PR."""


class GitHubPullRequestGateway(Protocol):
    def find_open(
        self,
        repository_full_name: str,
        head_branch: str,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> Sequence[PullRequestDescriptor]: ...
    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor: ...
    def create(
        self, repository_full_name: str, request: PullRequestCreateRequest
    ) -> PullRequestDescriptor: ...


class PullRequestLifecycleService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        workspace: WorkspaceService,
        github: GitHubPullRequestGateway,
    ) -> None:
        self.connection, self.workspace, self.github = connection, workspace, github
        self.records = SQLitePullRequestRepository(connection)

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
        now = now or datetime.now(UTC)
        evidence = self.connection.execute(
            """SELECT * FROM change_sets WHERE id=? AND project_id=? AND milestone_id=?
            AND decision='ACCEPT'""",
            (change_set_id, str(project_id), str(milestone_id)),
        ).fetchone()
        if evidence is None:
            raise PullRequestError(
                PullRequestFailure.PRECONDITION,
                "accepted M21 change-set evidence is required",
            )
        existing = self.connection.execute(
            "SELECT * FROM commits WHERE change_set_id=?", (change_set_id,)
        ).fetchall()
        if len(existing) > 1:
            raise PullRequestError(
                PullRequestFailure.PRECONDITION,
                "accepted change set has conflicting trusted commits",
            )
        if existing:
            commit_row = existing[0]
            if (
                commit_row["project_id"] != str(project_id)
                or commit_row["milestone_id"] != str(milestone_id)
                or commit_row["worktree_id"] != evidence["worktree_id"]
                or commit_row["branch_name"] != evidence["branch_name"]
                or commit_row["validated_diff_hash"] != evidence["diff_hash"]
                or commit_row["parent_sha"] != evidence["head_sha_before_commit"]
            ):
                raise PullRequestError(
                    PullRequestFailure.PRECONDITION,
                    "trusted commit identity conflicts with accepted change set",
                )
            commit_sha = commit_row["commit_sha"]
        else:
            files = json.loads(evidence["files_json"])
            commit = self.workspace.commit(
                project_id,
                milestone_id,
                evidence["head_sha_before_commit"],
                [item["path"] for item in files],
                commit_message or f"Apply accepted ChangeSet {change_set_id}",
                now,
                expected_diff_hash=evidence["diff_hash"],
            )
            commit_sha = commit.commit_sha
        return self.establish_for_commit(
            project_id,
            milestone_id,
            commit_sha,
            change_set_id,
            correlation_id,
            now=now,
        )

    def establish_for_commit(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        commit_sha: str,
        change_set_id: str,
        correlation_id: str,
        *,
        now: datetime | None = None,
    ) -> PullRequestRecord:
        """Host/recovery seam for an already-created trusted M21 commit."""
        now = now or datetime.now(UTC)
        commit_evidence = self.connection.execute(
            """SELECT 1 FROM commits c JOIN change_sets cs ON cs.id=c.change_set_id
            WHERE c.commit_sha=? AND c.project_id=? AND c.milestone_id=?
              AND cs.id=? AND cs.decision='ACCEPT'
              AND c.validated_diff_hash=cs.diff_hash""",
            (commit_sha, str(project_id), str(milestone_id), change_set_id),
        ).fetchone()
        if commit_evidence is None:
            raise PullRequestError(
                PullRequestFailure.PRECONDITION,
                "trusted commit is not bound to accepted M21 evidence",
            )
        inspection = self.workspace.inspect(project_id, milestone_id, now)
        if inspection.head_sha != commit_sha:
            raise PullRequestError(
                PullRequestFailure.HEAD_SHA_MISMATCH,
                "trusted local HEAD differs from expected commit",
            )
        repo = self.connection.execute(
            """SELECT gr.* FROM github_repositories gr JOIN git_repositories git
            ON git.github_repository_id=gr.id JOIN git_workspaces w ON w.git_repository_id=git.id
            WHERE gr.project_id=? AND w.milestone_id=? AND gr.status='VERIFIED'""",
            (str(project_id), str(milestone_id)),
        ).fetchone()
        milestone = self.connection.execute(
            "SELECT sequence_number,title,code FROM milestones WHERE id=? AND project_id=?",
            (str(milestone_id), str(project_id)),
        ).fetchone()
        if repo is None or milestone is None or repo["external_repository_id"] is None:
            raise PullRequestError(
                PullRequestFailure.REPOSITORY_IDENTITY_MISMATCH,
                "verified repository and milestone identity required",
            )
        if repo["default_branch"] != inspection.workspace.base_branch:
            raise PullRequestError(
                PullRequestFailure.BRANCH_MISMATCH,
                "workspace base differs from verified default branch",
            )
        pushed = self.workspace.push(project_id, milestone_id, commit_sha, now)
        if pushed.remote_sha != commit_sha:
            raise PullRequestError(
                PullRequestFailure.HEAD_SHA_MISMATCH,
                "remote milestone branch was not verified",
            )
        title = f"M{milestone['sequence_number']}: {milestone['title']}"[:256]
        body = (
            "This pull request is managed by Syntra Build.\n\n"
            f"Milestone: {milestone['code']} — {milestone['title']}\n"
            f"Trusted head: `{commit_sha}`\nChangeSet: `{change_set_id}`\n"
        )
        request = PullRequestCreateRequest(
            PULL_REQUEST_INTERFACE_VERSION,
            correlation_id,
            project_id,
            milestone_id,
            repo["external_repository_id"],
            inspection.workspace.branch_name,
            repo["default_branch"],
            commit_sha,
            title,
            body,
        )
        try:
            with transaction(self.connection):
                self.records.ensure_intent(
                    str(uuid4()),
                    repo["id"],
                    request,
                    sha256(body.encode()).hexdigest(),
                    now,
                )
        except PullRequestPersistenceConflict as error:
            raise PullRequestError(
                PullRequestFailure.PR_IDENTITY_MISMATCH, str(error)
            ) from error
        try:
            candidate = self._create_or_get(repo["full_name"], request, now)
        except PullRequestError as error:
            if error.failure in {
                PullRequestFailure.PR_COLLISION,
                PullRequestFailure.PR_IDENTITY_MISMATCH,
            }:
                with transaction(self.connection):
                    self.records.mark_intent(milestone_id, "BLOCKED", now)
            raise
        live = self.github.get(
            repo["full_name"], candidate.pull_request_number, project_id, milestone_id
        )
        try:
            self._verify(request, live)
        except PullRequestError as error:
            if error.failure not in {
                PullRequestFailure.PR_CLOSED,
                PullRequestFailure.PR_MERGED_UNEXPECTEDLY,
            }:
                raise
            # Preserve independently observed terminal state for later recovery;
            # M22 still fails closed and never creates a replacement.
            try:
                with transaction(self.connection):
                    self.records.save_verified(
                        str(uuid4()), repo["id"], live, title, now
                    )
            except PullRequestPersistenceConflict as conflict:
                raise PullRequestError(
                    PullRequestFailure.PR_IDENTITY_MISMATCH, str(conflict)
                ) from conflict
            raise
        try:
            with transaction(self.connection):
                return self.records.save_verified(
                    str(uuid4()), repo["id"], live, title, now
                )
        except PullRequestPersistenceConflict as error:
            raise PullRequestError(
                PullRequestFailure.PR_IDENTITY_MISMATCH, str(error)
            ) from error

    def _create_or_get(
        self, full_name: str, request: PullRequestCreateRequest, now: datetime
    ) -> PullRequestDescriptor:
        persisted = self.records.for_milestone(request.milestone_id)
        if persisted is not None:
            # A known implementation PR is authoritative ownership evidence even
            # when GitHub no longer lists it as open. Observe it; never replace it.
            return self.github.get(
                full_name,
                persisted.external_pr_number,
                request.project_id,
                request.milestone_id,
            )
        existing = list(
            self.github.find_open(
                full_name, request.head_branch, request.project_id, request.milestone_id
            )
        )
        matches = [
            item
            for item in existing
            if item.repository_id == request.repository_id
            and item.head_branch == request.head_branch
            and item.base_branch == request.base_branch
        ]
        if len(matches) > 1:
            raise PullRequestError(
                PullRequestFailure.PR_COLLISION,
                "multiple matching pull requests require reconciliation",
            )
        if len(matches) == 1:
            return matches[0]
        if existing:
            raise PullRequestError(
                PullRequestFailure.PR_IDENTITY_MISMATCH,
                "milestone branch is owned by a conflicting pull request",
            )
        try:
            return self.github.create(full_name, request)
        except PullRequestCreateConflict:
            matched = self._reconcile_after_conflict(full_name, request)
            if matched is not None:
                return matched
            raise PullRequestError(
                PullRequestFailure.PROVIDER_REJECTION,
                "pull request creation conflicted without matching evidence",
            ) from None
        except AmbiguousGitHubResult:
            with transaction(self.connection):
                self.records.mark_intent(request.milestone_id, "AMBIGUOUS", now)
            reconciled = list(
                self.github.find_open(
                    full_name,
                    request.head_branch,
                    request.project_id,
                    request.milestone_id,
                )
            )
            matches = [
                item
                for item in reconciled
                if item.repository_id == request.repository_id
                and item.head_branch == request.head_branch
                and item.base_branch == request.base_branch
            ]
            if len(matches) == 1:
                return matches[0]
            if reconciled:
                with transaction(self.connection):
                    self.records.mark_intent(request.milestone_id, "BLOCKED", now)
                raise PullRequestError(
                    PullRequestFailure.PR_COLLISION,
                    "ambiguous create found conflicting pull requests",
                ) from None
            # A confirmed absence permits exactly one bounded retry.
            try:
                return self.github.create(full_name, request)
            except PullRequestCreateConflict:
                matched = self._reconcile_after_conflict(full_name, request)
                if matched is not None:
                    return matched
                raise PullRequestError(
                    PullRequestFailure.PROVIDER_REJECTION,
                    "pull request creation conflicted without matching evidence",
                ) from None
            except AmbiguousGitHubResult as error:
                raise PullRequestError(
                    PullRequestFailure.AMBIGUOUS,
                    "bounded pull request creation retry remained ambiguous",
                ) from error

    def _reconcile_after_conflict(
        self, full_name: str, request: PullRequestCreateRequest
    ) -> PullRequestDescriptor | None:
        observed = list(
            self.github.find_open(
                full_name,
                request.head_branch,
                request.project_id,
                request.milestone_id,
            )
        )
        matches = [
            item
            for item in observed
            if item.repository_id == request.repository_id
            and item.head_branch == request.head_branch
            and item.base_branch == request.base_branch
        ]
        if len(matches) > 1:
            raise PullRequestError(
                PullRequestFailure.PR_COLLISION,
                "create conflict found multiple matching pull requests",
            )
        if len(matches) == 1 and len(observed) == 1:
            return matches[0]
        if observed:
            raise PullRequestError(
                PullRequestFailure.PR_IDENTITY_MISMATCH,
                "create conflict found incompatible pull request evidence",
            )
        return None

    @staticmethod
    def _verify(
        expected: PullRequestCreateRequest, actual: PullRequestDescriptor
    ) -> None:
        if (
            actual.repository_id != expected.repository_id
            or actual.project_id != expected.project_id
            or actual.milestone_id != expected.milestone_id
        ):
            raise PullRequestError(
                PullRequestFailure.PR_IDENTITY_MISMATCH,
                "live pull request ownership differs",
            )
        if (
            actual.head_branch != expected.head_branch
            or actual.base_branch != expected.base_branch
        ):
            raise PullRequestError(
                PullRequestFailure.BRANCH_MISMATCH, "live pull request branches differ"
            )
        if actual.head_sha != expected.head_sha:
            raise PullRequestError(
                PullRequestFailure.HEAD_SHA_MISMATCH,
                "live pull request head differs from trusted push",
            )
        if actual.state is PullRequestState.CLOSED:
            raise PullRequestError(
                PullRequestFailure.PR_CLOSED,
                "implementation pull request is unexpectedly closed",
            )
        if actual.state is PullRequestState.MERGED:
            raise PullRequestError(
                PullRequestFailure.PR_MERGED_UNEXPECTEDLY,
                "implementation pull request is unexpectedly merged",
            )
