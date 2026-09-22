"""SQLite persistence for M19 repositories, workspaces, and trusted commits."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.workspaces import (
    ManagedRepository,
    TrustedCommit,
    Workspace,
    WorkspaceError,
    WorkspaceState,
)


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


class SQLiteWorkspaceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def managed_for_project(self, project_id: ProjectId) -> ManagedRepository | None:
        row = self.connection.execute(
            "SELECT * FROM git_repositories WHERE project_id=?", (str(project_id),)
        ).fetchone()
        if row is None:
            return None
        return ManagedRepository(
            row["id"],
            project_id,
            row["github_repository_id"],
            Path(row["repository_path"]),
            row["remote_url"],
            row["default_branch"],
            _time(row["last_fetch_at"]),
            row["last_known_main_sha"],
        )

    def create_managed(self, repository: ManagedRepository, now: datetime) -> None:
        self.connection.execute(
            """INSERT INTO git_repositories
            (id,project_id,github_repository_id,repository_path,remote_name,remote_url,
             default_branch,created_at,updated_at) VALUES (?,?,?,?,? ,?,?,?,?)""",
            (
                repository.id,
                str(repository.project_id),
                repository.github_repository_id,
                str(repository.path),
                "origin",
                repository.remote_url,
                repository.default_branch,
                now.isoformat(),
                now.isoformat(),
            ),
        )

    def record_fetch(self, repository_id: str, sha: str, now: datetime) -> None:
        self.connection.execute(
            """UPDATE git_repositories
            SET last_fetch_at=?,last_known_main_sha=?,updated_at=?
            WHERE id=?""",
            (now.isoformat(), sha, now.isoformat(), repository_id),
        )

    def workspace_for_milestone(self, milestone_id: MilestoneId) -> Workspace | None:
        row = self.connection.execute(
            "SELECT * FROM git_workspaces WHERE milestone_id=?", (str(milestone_id),)
        ).fetchone()
        return None if row is None else self._workspace(row)

    def create_workspace(self, workspace: Workspace) -> None:
        self.connection.execute(
            """INSERT INTO git_workspaces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                workspace.id,
                str(workspace.project_id),
                str(workspace.milestone_id),
                workspace.git_repository_id,
                workspace.branch_name,
                str(workspace.path),
                workspace.base_branch,
                workspace.base_sha,
                workspace.current_head_sha,
                workspace.state.value,
                workspace.created_at.isoformat(),
                workspace.last_validated_at.isoformat()
                if workspace.last_validated_at
                else None,
                workspace.removed_at.isoformat() if workspace.removed_at else None,
            ),
        )

    def update_workspace(
        self,
        workspace_id: str,
        state: WorkspaceState,
        head: str | None,
        now: datetime,
        *,
        removed: bool = False,
    ) -> None:
        changed = self.connection.execute(
            """UPDATE git_workspaces
            SET state=?,current_head_sha=COALESCE(?,current_head_sha),
            last_validated_at=?,
            removed_at=CASE WHEN ? THEN ? ELSE removed_at END WHERE id=?""",
            (
                state.value,
                head,
                now.isoformat(),
                removed,
                now.isoformat(),
                workspace_id,
            ),
        ).rowcount
        if changed != 1:
            raise WorkspaceError("workspace persistence identity was not found")

    def save_commit(
        self,
        commit: TrustedCommit,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> None:
        self.connection.execute(
            """INSERT INTO commits VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                commit.id,
                str(project_id),
                str(milestone_id),
                commit.workspace_id,
                commit.commit_sha,
                commit.parent_sha,
                commit.branch_name,
                commit.message,
                "Syntra Build",
                "syntra@localhost",
                commit.created_at.isoformat(),
            ),
        )

    def mark_pushed(self, commit_sha: str, now: datetime) -> None:
        self.connection.execute(
            "UPDATE commits SET pushed_at=COALESCE(pushed_at,?) WHERE commit_sha=?",
            (now.isoformat(), commit_sha),
        )

    @staticmethod
    def _workspace(row: sqlite3.Row) -> Workspace:
        created = _time(row["created_at"])
        assert created is not None
        return Workspace(
            row["id"],
            ProjectId.from_string(row["project_id"]),
            MilestoneId.from_string(row["milestone_id"]),
            row["git_repository_id"],
            row["branch_name"],
            Path(row["worktree_path"]),
            row["base_branch"],
            row["base_sha"],
            row["current_head_sha"],
            WorkspaceState(row["state"]),
            created,
            _time(row["last_validated_at"]),
            _time(row["removed_at"]),
        )
