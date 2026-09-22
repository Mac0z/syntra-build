from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain import MilestoneId, ProjectId
from syntra_build.domain.workspaces import (
    AmbiguousPushError,
    PushNotAppliedError,
    TrustedCommit,
    WorkspaceError,
    WorkspaceState,
)
from syntra_build.infrastructure.persistence import apply_migrations, open_database

NOW = datetime(2026, 9, 22, tzinfo=UTC)
PID = ProjectId(UUID(int=201))
MID = MilestoneId(UUID(int=202))
BASE = "1" * 40
LATER = "2" * 40
UNEXPECTED = "3" * 40
BRANCH = "syntra/m01-foundation"


class FakeGit:
    def __init__(self) -> None:
        self.branches: dict[str, str] = {}
        self.remote_branches: dict[str, str] = {}
        self.commits = {BASE}
        self.registered_paths: set[Path] = set()
        self.worktree_branches: dict[Path, str] = {}
        self.push_outcome: str | None = None
        self.push_calls = 0

    def ensure_bare(self, path: Path, remote_url: str) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def fetch(self, path: Path, remote_url: str, branch: str) -> str:
        return BASE

    def branch_sha(self, repository: Path, branch: str) -> str | None:
        return self.branches.get(branch)

    def has_commit(self, repository: Path, sha: str) -> bool:
        return sha in self.commits

    def fetch_branch(self, repository: Path, remote_url: str, branch: str) -> str:
        sha = self.remote_branches[branch]
        self.commits.add(sha)
        return sha

    def create_branch(self, repository: Path, branch: str, base_sha: str) -> None:
        assert base_sha in self.commits
        self.branches[branch] = base_sha

    def add_worktree(self, repository: Path, path: Path, branch: str) -> None:
        path.mkdir(parents=True)
        self.registered_paths.add(path)
        self.worktree_branches[path] = branch

    def registered(self, repository: Path, path: Path) -> bool:
        return path in self.registered_paths

    def prune_worktrees(self, repository: Path) -> None:
        self.registered_paths = {
            path for path in self.registered_paths if path.exists()
        }

    def origin(self, worktree: Path) -> str:
        return "https://github.com/owner/repo.git"

    def branch(self, worktree: Path) -> str:
        return self.worktree_branches[worktree]

    def head(self, worktree: Path) -> str:
        return self.branches[self.worktree_branches[worktree]]

    def changes(
        self, worktree: Path
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        untracked = ("dirty.txt",) if (worktree / "dirty.txt").exists() else ()
        return (), (), untracked

    def remote_branch_sha(
        self, repository: Path, remote_url: str, branch: str
    ) -> str | None:
        return self.remote_branches.get(branch)

    def push(self, repository: Path, remote_url: str, branch: str) -> None:
        self.push_calls += 1
        local = self.branches[branch]
        if self.push_outcome == "updated_then_raised":
            self.remote_branches[branch] = local
            raise AmbiguousPushError("ambiguous")
        if self.push_outcome == "raised":
            raise AmbiguousPushError("ambiguous")
        if self.push_outcome == "unexpected":
            self.remote_branches[branch] = UNEXPECTED
            raise AmbiguousPushError("ambiguous")
        self.remote_branches[branch] = local

    def remove_worktree(self, repository: Path, path: Path) -> None:
        self.registered_paths.remove(path)
        path.rmdir()


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


def prepared(tmp_path: Path) -> tuple[sqlite3.Connection, FakeGit, WorkspaceService]:
    db = database(tmp_path)
    git = FakeGit()
    service = WorkspaceService(db, git, tmp_path / "data")  # type: ignore[arg-type]
    service.prepare_workspace(PID, MID, NOW)
    return db, git, service


def advance_head(
    db: sqlite3.Connection, git: FakeGit, service: WorkspaceService
) -> None:
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    git.commits.add(LATER)
    git.branches[BRANCH] = LATER
    service.records.update_workspace(workspace.id, WorkspaceState.READY, LATER, NOW)
    db.commit()


def test_fresh_and_restart_reuse_preserve_base(tmp_path: Path) -> None:
    db, git, service = prepared(tmp_path)
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    assert git.branches[BRANCH] == workspace.base_sha == BASE

    restarted = WorkspaceService(db, git, tmp_path / "data")  # type: ignore[arg-type]
    assert restarted.prepare_workspace(PID, MID, NOW).current_head_sha == BASE


def test_missing_worktree_registration_is_reconciled(tmp_path: Path) -> None:
    db, git, service = prepared(tmp_path)
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    workspace.path.rmdir()
    assert git.registered(Path(), workspace.path)

    recovered = service.prepare_workspace(PID, MID, NOW)

    assert recovered.current_head_sha == BASE
    assert recovered.path.exists()


def test_unowned_directory_and_wrong_branch_are_rejected(tmp_path: Path) -> None:
    db = database(tmp_path)
    git = FakeGit()
    service = WorkspaceService(db, git, tmp_path / "data")  # type: ignore[arg-type]
    service._workspace_path(PID, MID).mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="will not be adopted"):
        service.prepare_workspace(PID, MID, NOW)
    db.close()

    other = tmp_path / "other"
    other.mkdir()
    db, git, service = prepared(other)
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    git.branches["wrong"] = BASE
    git.worktree_branches[workspace.path] = "wrong"
    with pytest.raises(WorkspaceError, match="identity differs"):
        service.inspect(PID, MID, NOW)


def test_dirty_workspace_is_preserved(tmp_path: Path) -> None:
    _, _, service = prepared(tmp_path)
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    (workspace.path / "dirty.txt").write_text("keep")
    assert service.inspect(PID, MID, NOW).workspace.state is WorkspaceState.DIRTY
    with pytest.raises(WorkspaceError, match="dirty"):
        service.remove_workspace(PID, MID, NOW)
    assert (workspace.path / "dirty.txt").read_text() == "keep"


def test_fresh_workspace_with_unexpected_head_fails_without_recording_it(
    tmp_path: Path,
) -> None:
    db, git, service = prepared(tmp_path)
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    # Model the persist-before-side-effect window before initial validation.
    db.execute(
        "UPDATE git_workspaces SET current_head_sha=NULL,state='ACTIVE' WHERE id=?",
        (workspace.id,),
    )
    db.commit()
    git.commits.add(UNEXPECTED)
    git.branches[BRANCH] = UNEXPECTED

    with pytest.raises(WorkspaceError, match="HEAD differs"):
        service.inspect(PID, MID, NOW)

    persisted = service.records.workspace_for_milestone(MID)
    assert persisted is not None
    assert persisted.state is WorkspaceState.ERROR
    assert persisted.current_head_sha is None
    assert persisted.base_sha == BASE


def test_unexpected_local_history_is_not_adopted_by_inspect_commit_or_push(
    tmp_path: Path,
) -> None:
    db, git, service = prepared(tmp_path)
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None and workspace.current_head_sha == BASE
    git.commits.add(UNEXPECTED)
    git.branches[BRANCH] = UNEXPECTED
    (workspace.path / "change.txt").write_text("not trusted")

    for operation in (
        lambda: service.inspect(PID, MID, NOW),
        lambda: service.commit(PID, MID, UNEXPECTED, ["change.txt"], "unsafe", NOW),
        lambda: service.push(PID, MID, UNEXPECTED, NOW),
    ):
        with pytest.raises(WorkspaceError, match="HEAD differs"):
            operation()
        persisted = service.records.workspace_for_milestone(MID)
        assert persisted is not None
        assert persisted.state is WorkspaceState.ERROR
        assert persisted.current_head_sha == BASE

    assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 0
    assert git.push_calls == 0


def test_missing_branch_recovers_exact_persisted_head_from_remote(
    tmp_path: Path,
) -> None:
    db, git, service = prepared(tmp_path)
    advance_head(db, git, service)
    del git.branches[BRANCH]
    git.commits.remove(LATER)
    git.remote_branches[BRANCH] = LATER
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    workspace.path.rmdir()
    git.registered_paths.remove(workspace.path)

    recovered = service.prepare_workspace(PID, MID, NOW)

    assert git.branches[BRANCH] == LATER
    assert recovered.current_head_sha == LATER


def test_unrecoverable_head_fails_without_regressing_evidence(tmp_path: Path) -> None:
    db, git, service = prepared(tmp_path)
    advance_head(db, git, service)
    del git.branches[BRANCH]
    git.commits.remove(LATER)
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    workspace.path.rmdir()
    git.registered_paths.remove(workspace.path)

    with pytest.raises(WorkspaceError, match="cannot be recovered"):
        service.prepare_workspace(PID, MID, NOW)

    persisted = service.records.workspace_for_milestone(MID)
    assert persisted is not None
    assert persisted.current_head_sha == LATER
    assert persisted.base_sha == BASE


def test_removed_workspace_is_terminal(tmp_path: Path) -> None:
    _, _, service = prepared(tmp_path)
    removed = service.remove_workspace(PID, MID, NOW)
    assert removed.state is WorkspaceState.REMOVED and removed.removed_at is not None

    with pytest.raises(WorkspaceError, match="cannot be implicitly recreated"):
        service.prepare_workspace(PID, MID, NOW)
    still_removed = service.records.workspace_for_milestone(MID)
    assert still_removed is not None and still_removed.state is WorkspaceState.REMOVED


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("raised", PushNotAppliedError),
        ("unexpected", WorkspaceError),
    ],
)
def test_ambiguous_push_not_updated_or_unexpected_fails_closed(
    tmp_path: Path, outcome: str, error: type[WorkspaceError]
) -> None:
    db, git, service = prepared(tmp_path)
    advance_head(db, git, service)
    git.push_outcome = outcome
    with pytest.raises(error):
        service.push(PID, MID, LATER, NOW)
    assert git.push_calls == 1


def test_ambiguous_push_reconciles_updated_remote_and_persists(tmp_path: Path) -> None:
    db, git, service = prepared(tmp_path)
    advance_head(db, git, service)
    workspace = service.records.workspace_for_milestone(MID)
    assert workspace is not None
    service.records.save_commit(
        TrustedCommit("commit", workspace.id, LATER, BASE, BRANCH, "message", NOW),
        PID,
        MID,
    )
    db.commit()
    git.push_outcome = "updated_then_raised"

    result = service.push(PID, MID, LATER, NOW)

    assert result.remote_sha == LATER and git.push_calls == 1
    assert (
        db.execute(
            "SELECT pushed_at FROM commits WHERE commit_sha=?", (LATER,)
        ).fetchone()[0]
        is not None
    )
