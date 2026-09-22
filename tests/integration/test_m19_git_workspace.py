from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.domain import MilestoneId, ProjectId
from syntra_build.domain.workspaces import (
    ManagedRepository,
    TrustedCommit,
    Workspace,
    WorkspaceError,
    WorkspaceState,
)
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import apply_migrations, open_database
from syntra_build.infrastructure.persistence.workspaces import SQLiteWorkspaceRepository

NOW = datetime(2026, 9, 22, tzinfo=UTC)
PID = ProjectId(UUID(int=100))
MID = MilestoneId(UUID(int=101))
SHA = "a" * 40


def run(path: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def seeded_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    run(remote, "init", "--bare")
    seed = tmp_path / "seed"
    seed.mkdir()
    run(seed, "init", "--initial-branch=main")
    (seed / "README.md").write_text("initial\n")
    run(seed, "add", "README.md")
    identity = dict(
        os.environ,
        GIT_AUTHOR_NAME="seed",
        GIT_AUTHOR_EMAIL="seed@example.test",
        GIT_COMMITTER_NAME="seed",
        GIT_COMMITTER_EMAIL="seed@example.test",
    )
    run(seed, "commit", "-m", "initial", env=identity)
    run(seed, "remote", "add", "origin", str(remote))
    run(seed, "push", "origin", "main")
    return remote


def test_bare_repository_worktree_commit_push_and_dirty_preservation(
    tmp_path: Path,
) -> None:
    remote = seeded_remote(tmp_path)
    git = TrustedGit(tmp_path / "auth")
    repository = tmp_path / "managed" / "repo.git"
    git.ensure_bare(repository, str(remote))
    base = git.fetch(repository, str(remote), "main")
    git.ensure_bare(repository, str(remote))
    assert git.origin(repository) == str(remote)

    branch = "syntra/m01-foundation"
    git.create_branch(repository, branch, base)
    workspace = tmp_path / "workspaces" / "project" / "milestone"
    git.add_worktree(repository, workspace, branch)
    assert git.registered(repository, workspace)
    assert git.head(workspace) == base

    (workspace / "new.txt").write_text("accepted\n")
    assert git.changes(workspace)[2] == ("new.txt",)
    sha, parent = git.commit(workspace, ["new.txt"], "trusted commit")
    assert parent == base and sha != base
    assert run(workspace, "show", "-s", "--format=%an <%ae>") == (
        "Syntra Build <syntra@localhost>"
    )
    git.push(repository, str(remote), branch)
    assert git.remote_branch_sha(repository, str(remote), branch) == sha
    assert "force" not in run(workspace, "reflog", "-1")

    (workspace / "dirty.txt").write_text("preserve me")
    with pytest.raises(WorkspaceError):
        git.remove_worktree(repository, workspace)
    assert (workspace / "dirty.txt").read_text() == "preserve me"


def database(tmp_path: Path) -> sqlite3.Connection:
    db = open_database(tmp_path / "state.db")
    apply_migrations(db)
    db.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at)
        VALUES (?,?,'READY',?,?,?)""",
        (str(PID), "Project", NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    db.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,created_at,updated_at)
        VALUES (?,?,1,'M1','Foundation','READY',?,?)""",
        (str(MID), str(PID), NOW.isoformat(), NOW.isoformat()),
    )
    db.execute(
        """INSERT INTO github_repositories
        (id,project_id,provider,owner,repository_name,full_name,
         external_repository_id,visibility,default_branch,status,created_at,updated_at,
         verified_at) VALUES ('gh',?,'github','owner','repo','owner/repo',1,'public',
         'main','VERIFIED',?,?,?)""",
        (str(PID), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    db.commit()
    return db


def test_migration_015_round_trip_and_immutable_identity(tmp_path: Path) -> None:
    with database(tmp_path) as db:
        records = SQLiteWorkspaceRepository(db)
        managed = ManagedRepository(
            "managed",
            PID,
            "gh",
            tmp_path / "repo.git",
            "https://github.com/owner/repo.git",
            "main",
            None,
            None,
        )
        records.create_managed(managed, NOW)
        workspace = Workspace(
            "workspace",
            PID,
            MID,
            managed.id,
            "syntra/m01-foundation",
            tmp_path / "workspace",
            "main",
            SHA,
            SHA,
            WorkspaceState.READY,
            NOW,
        )
        records.create_workspace(workspace)
        commit = TrustedCommit(
            "commit",
            workspace.id,
            "b" * 40,
            SHA,
            workspace.branch_name,
            "trusted commit",
            NOW,
        )
        records.save_commit(commit, PID, MID)
        db.commit()

        assert records.managed_for_project(PID) == managed
        assert records.workspace_for_milestone(MID) == workspace
        row = db.execute("SELECT * FROM commits WHERE id='commit'").fetchone()
        assert row["author_name"] == "Syntra Build"
        assert row["pushed_at"] is None
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE git_workspaces SET base_sha=?", ("c" * 40,))
