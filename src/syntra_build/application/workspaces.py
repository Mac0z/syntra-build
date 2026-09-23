"""M19 orchestration for managed repositories and trusted milestone workspaces."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.workspaces import (
    AmbiguousPushError,
    ManagedRepository,
    PushNotAppliedError,
    TrustedCommit,
    TrustedPushResult,
    Workspace,
    WorkspaceError,
    WorkspaceInspection,
    WorkspaceState,
)
from syntra_build.infrastructure.change_validation import ChangeCollector
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.change_validation import (
    SQLiteValidationRepository,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.workspaces import SQLiteWorkspaceRepository

_MAX_BRANCH = 120


def milestone_branch_name(sequence: int, title: str) -> str:
    """Return the stable, bounded and ref-safe Syntra branch name."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "milestone"
    prefix = f"syntra/m{sequence:02d}-"
    return prefix + slug[: _MAX_BRANCH - len(prefix)].rstrip("-")


class WorkspaceService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        git: TrustedGit,
        data_root: Path,
    ) -> None:
        self.connection, self.git = connection, git
        self.data_root = data_root.resolve()
        self.repository_root = (self.data_root / "repositories").resolve()
        self.workspace_root = (self.data_root / "workspaces").resolve()
        self.records = SQLiteWorkspaceRepository(connection)

    @staticmethod
    def _contained(root: Path, candidate: Path) -> Path:
        resolved = candidate.resolve(strict=False)
        if resolved == root or root not in resolved.parents:
            raise WorkspaceError("managed path escapes its configured root")
        return resolved

    def _repository_path(self, project_id: ProjectId) -> Path:
        return self._contained(
            self.repository_root, self.repository_root / str(project_id) / "repo.git"
        )

    def _workspace_path(self, project_id: ProjectId, milestone_id: MilestoneId) -> Path:
        return self._contained(
            self.workspace_root,
            self.workspace_root / str(project_id) / str(milestone_id),
        )

    def _github_identity(self, project_id: ProjectId) -> sqlite3.Row:
        row = self.connection.execute(
            """SELECT * FROM github_repositories
            WHERE project_id=? AND status='VERIFIED'
              AND external_repository_id IS NOT NULL""",
            (str(project_id),),
        ).fetchone()
        if row is None or row["default_branch"] != "main":
            raise WorkspaceError("a verified M18 main-branch repository is required")
        return cast(sqlite3.Row, row)

    def synchronise_repository(
        self,
        project_id: ProjectId,
        now: datetime | None = None,
    ) -> ManagedRepository:
        now = now or datetime.now(UTC)
        github = self._github_identity(project_id)
        expected_path = self._repository_path(project_id)
        remote_url = f"https://github.com/{github['full_name']}.git"
        if "@" in remote_url.partition("://")[2].partition("/")[0]:
            raise WorkspaceError("credential-bearing Git remote rejected")
        managed = self.records.managed_for_project(project_id)
        if managed is None:
            managed = ManagedRepository(
                str(uuid4()),
                project_id,
                github["id"],
                expected_path,
                remote_url,
                github["default_branch"],
                None,
                None,
            )
            with transaction(self.connection):
                self.records.create_managed(managed, now)
        elif (
            managed.github_repository_id != github["id"]
            or managed.path != expected_path
            or managed.remote_url != remote_url
            or managed.default_branch != github["default_branch"]
        ):
            raise WorkspaceError("persisted managed repository identity differs")
        self.git.ensure_bare(managed.path, managed.remote_url)
        sha = self.git.fetch(managed.path, managed.remote_url, managed.default_branch)
        with transaction(self.connection):
            self.records.record_fetch(managed.id, sha, now)
        refreshed = self.records.managed_for_project(project_id)
        assert refreshed is not None
        return refreshed

    def prepare_workspace(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        now: datetime | None = None,
    ) -> Workspace:
        now = now or datetime.now(UTC)
        milestone = SQLiteMilestoneRepository(
            self.connection, lambda: str(uuid4())
        ).get(milestone_id, project_id)
        prior_workspace = self.records.workspace_for_milestone(milestone_id)
        if (
            prior_workspace is not None
            and prior_workspace.state is WorkspaceState.REMOVED
        ):
            raise WorkspaceError("removed workspace cannot be implicitly recreated")
        managed = self.synchronise_repository(project_id, now)
        assert managed.last_known_main_sha is not None
        branch = milestone_branch_name(milestone.sequence_number, milestone.title)
        expected_path = self._workspace_path(project_id, milestone_id)
        workspace = prior_workspace
        if workspace is None:
            if expected_path.exists():
                raise WorkspaceError(
                    "unowned workspace path exists and will not be adopted"
                )
            workspace = Workspace(
                str(uuid4()),
                project_id,
                milestone_id,
                managed.id,
                branch,
                expected_path,
                managed.default_branch,
                managed.last_known_main_sha,
                None,
                WorkspaceState.ACTIVE,
                now,
            )
            with transaction(self.connection):
                self.records.create_workspace(workspace)
        elif (
            workspace.project_id != project_id
            or workspace.git_repository_id != managed.id
            or workspace.branch_name != branch
            or workspace.path != expected_path
            or workspace.base_branch != managed.default_branch
        ):
            raise WorkspaceError("persisted workspace identity differs")

        local_sha = self.git.branch_sha(managed.path, workspace.branch_name)
        if local_sha is None:
            expected_sha = workspace.current_head_sha or workspace.base_sha
            remote_sha = self.git.remote_branch_sha(
                managed.path, managed.remote_url, workspace.branch_name
            )
            if remote_sha is not None and remote_sha != expected_sha:
                self._mark(workspace, WorkspaceState.ERROR, None, now)
                raise WorkspaceError("remote milestone branch has unexpected HEAD")
            if not self.git.has_commit(managed.path, expected_sha):
                if remote_sha != expected_sha:
                    self._mark(workspace, WorkspaceState.ERROR, None, now)
                    raise WorkspaceError("persisted workspace HEAD cannot be recovered")
                fetched = self.git.fetch_branch(
                    managed.path, managed.remote_url, workspace.branch_name
                )
                if fetched != expected_sha or not self.git.has_commit(
                    managed.path, expected_sha
                ):
                    self._mark(workspace, WorkspaceState.ERROR, None, now)
                    raise WorkspaceError("persisted workspace HEAD cannot be recovered")
            self.git.create_branch(managed.path, workspace.branch_name, expected_sha)
        elif workspace.current_head_sha and local_sha != workspace.current_head_sha:
            self._mark(workspace, WorkspaceState.ERROR, local_sha, now)
            raise WorkspaceError(
                "milestone branch HEAD differs from persisted evidence"
            )
        elif workspace.current_head_sha is None and local_sha != workspace.base_sha:
            self._mark(workspace, WorkspaceState.ERROR, local_sha, now)
            raise WorkspaceError("milestone branch does not start at recorded base")

        if workspace.path.exists():
            if not self.git.registered(managed.path, workspace.path):
                self._mark(workspace, WorkspaceState.ORPHANED, None, now)
                raise WorkspaceError(
                    "workspace exists without managed Git registration"
                )
        else:
            if self.git.registered(managed.path, workspace.path):
                self.git.prune_worktrees(managed.path)
            self.git.add_worktree(managed.path, workspace.path, workspace.branch_name)
        return self.inspect(project_id, milestone_id, now).workspace

    def inspect(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        now: datetime | None = None,
    ) -> WorkspaceInspection:
        now = now or datetime.now(UTC)
        managed = self.records.managed_for_project(project_id)
        workspace = self.records.workspace_for_milestone(milestone_id)
        if managed is None or workspace is None or workspace.project_id != project_id:
            raise WorkspaceError("managed workspace does not exist")
        self._contained(self.workspace_root, workspace.path)
        if not workspace.path.exists() or not self.git.registered(
            managed.path, workspace.path
        ):
            self._mark(workspace, WorkspaceState.ORPHANED, None, now)
            raise WorkspaceError("workspace is missing or unregistered")
        origin = self.git.origin(workspace.path)
        branch, head = self.git.branch(workspace.path), self.git.head(workspace.path)
        if origin != managed.remote_url or branch != workspace.branch_name:
            self._mark(workspace, WorkspaceState.ERROR, None, now)
            raise WorkspaceError("workspace Git identity differs")
        expected_head = workspace.current_head_sha or workspace.base_sha
        if head != expected_head:
            # Observation must never turn an untrusted history change into
            # authoritative evidence. Only trusted commit() advances the HEAD.
            self._mark(workspace, WorkspaceState.ERROR, None, now)
            raise WorkspaceError("workspace HEAD differs from persisted evidence")
        tracked, staged, untracked = self.git.changes(workspace.path)
        state = (
            WorkspaceState.DIRTY
            if tracked or staged or untracked
            else WorkspaceState.READY
        )
        self._mark(workspace, state, head, now)
        refreshed = self.records.workspace_for_milestone(milestone_id)
        assert refreshed is not None
        return WorkspaceInspection(
            refreshed, origin, branch, head, True, tracked, staged, untracked
        )

    def _mark(
        self,
        workspace: Workspace,
        state: WorkspaceState,
        head: str | None,
        now: datetime,
    ) -> None:
        with transaction(self.connection):
            self.records.update_workspace(workspace.id, state, head, now)

    def commit(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        expected_head: str,
        paths: Iterable[str],
        message: str,
        now: datetime | None = None,
        *,
        expected_diff_hash: str | None = None,
    ) -> TrustedCommit:
        now = now or datetime.now(UTC)
        inspection = self.inspect(project_id, milestone_id, now)
        workspace = inspection.workspace
        if (
            inspection.head_sha != expected_head
            or inspection.current_branch != workspace.branch_name
        ):
            raise WorkspaceError("workspace HEAD or branch changed before commit")
        if expected_diff_hash is None:
            raise WorkspaceError("an accepted validated diff hash is required")
        trusted_head = workspace.current_head_sha or workspace.base_sha
        current = ChangeCollector().collect(workspace.path, trusted_head)
        validations = SQLiteValidationRepository(self.connection)
        if current.canonical_hash != expected_diff_hash:
            raise WorkspaceError("workspace diff changed after validation")
        evidence = validations.accepted_evidence(
            workspace.id, trusted_head, expected_diff_hash
        )
        if evidence is None:
            raise WorkspaceError(
                "diff hash has no accepted validation evidence for trusted HEAD"
            )
        selected_paths = tuple(paths)
        validated_paths = {
            item["path"]
            for item in cast(list[dict[str, object]], json.loads(evidence.files_json))
        }
        if set(selected_paths) != validated_paths or len(selected_paths) != len(
            validated_paths
        ):
            raise WorkspaceError("commit paths differ from the validated change set")
        sha, parent = self.git.commit(workspace.path, selected_paths, message)
        commit = TrustedCommit(
            str(uuid4()), workspace.id, sha, parent, workspace.branch_name, message, now
        )
        with transaction(self.connection):
            self.records.save_commit(
                commit, project_id, milestone_id, evidence.id, expected_diff_hash
            )
            self.records.update_workspace(workspace.id, WorkspaceState.READY, sha, now)
        return commit

    def push(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        expected_commit_sha: str,
        now: datetime | None = None,
    ) -> TrustedPushResult:
        now = now or datetime.now(UTC)
        inspection = self.inspect(project_id, milestone_id, now)
        workspace = inspection.workspace
        managed = self.records.managed_for_project(project_id)
        assert managed is not None
        if inspection.head_sha != expected_commit_sha:
            raise WorkspaceError("local commit SHA differs from expected push SHA")
        pre_push_sha = self.git.remote_branch_sha(
            managed.path, managed.remote_url, workspace.branch_name
        )
        remote_sha: str | None
        if pre_push_sha != expected_commit_sha:
            try:
                self.git.push(managed.path, managed.remote_url, workspace.branch_name)
            except AmbiguousPushError:
                reconciled_sha = self.git.remote_branch_sha(
                    managed.path, managed.remote_url, workspace.branch_name
                )
                if reconciled_sha == expected_commit_sha:
                    remote_sha = reconciled_sha
                elif reconciled_sha == pre_push_sha:
                    raise PushNotAppliedError(
                        "push did not update the expected remote branch"
                    ) from None
                else:
                    raise WorkspaceError(
                        "ambiguous push produced an unexpected remote branch SHA"
                    ) from None
            else:
                remote_sha = self.git.remote_branch_sha(
                    managed.path, managed.remote_url, workspace.branch_name
                )
        else:
            remote_sha = pre_push_sha
        if remote_sha != expected_commit_sha:
            raise WorkspaceError("remote branch verification was inconclusive")
        with transaction(self.connection):
            self.records.mark_pushed(expected_commit_sha, now)
        return TrustedPushResult(workspace.branch_name, expected_commit_sha, remote_sha)

    def remove_workspace(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        now: datetime | None = None,
    ) -> Workspace:
        now = now or datetime.now(UTC)
        managed = self.records.managed_for_project(project_id)
        workspace = self.records.workspace_for_milestone(milestone_id)
        if managed is None or workspace is None or workspace.project_id != project_id:
            raise WorkspaceError("managed workspace does not exist")
        self._contained(self.workspace_root, workspace.path)
        if workspace.path.exists():
            inspection = self.inspect(project_id, milestone_id, now)
            if not inspection.clean:
                raise WorkspaceError("dirty workspace removal is refused")
            self.git.remove_worktree(managed.path, workspace.path)
        elif self.git.registered(managed.path, workspace.path):
            self.git.prune_worktrees(managed.path)
        with transaction(self.connection):
            self.records.update_workspace(
                workspace.id,
                WorkspaceState.REMOVED,
                workspace.current_head_sha,
                now,
                removed=True,
            )
        result = self.records.workspace_for_milestone(milestone_id)
        assert result is not None
        return result
