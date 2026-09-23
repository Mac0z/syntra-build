from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from collections.abc import Generator, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from syntra_build.application.change_validation import ChangeValidationService
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.change_validation import FindingCode, ValidationDecision
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.workspaces import (
    ManagedRepository,
    Workspace,
    WorkspaceError,
    WorkspaceState,
)
from syntra_build.infrastructure.change_validation import ChangeCollector
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import apply_migrations, open_database
from syntra_build.infrastructure.persistence.change_validation import (
    SQLiteValidationRepository,
)
from syntra_build.infrastructure.persistence.workspaces import SQLiteWorkspaceRepository

NOW = datetime(2026, 9, 23, tzinfo=UTC)
PID = ProjectId(UUID(int=210))
MID = MilestoneId(UUID(int=211))


class MutateBeforeStageGit(TrustedGit):
    def stage(self, worktree: Path, paths: Iterable[str]) -> None:
        (worktree / "race.txt").write_text("content B\n")
        super().stage(worktree, paths)


class AlterIndexContentGit(TrustedGit):
    def stage(self, worktree: Path, paths: Iterable[str]) -> None:
        super().stage(worktree, paths)
        (worktree / "race.txt").write_text("altered index content\n")
        super().stage(worktree, ("race.txt",))


class AlterIndexPathsGit(TrustedGit):
    def stage(self, worktree: Path, paths: Iterable[str]) -> None:
        super().stage(worktree, paths)
        (worktree / "injected.txt").write_text("not validated\n")
        super().stage(worktree, ("injected.txt",))


class MutateAfterStageGit(TrustedGit):
    def stage(self, worktree: Path, paths: Iterable[str]) -> None:
        super().stage(worktree, paths)
        (worktree / "race.txt").write_text("content B\n")


def git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def validation_workspace(
    tmp_path: Path,
) -> Generator[tuple[sqlite3.Connection, Path, TrustedGit, ChangeValidationService]]:
    data = tmp_path / "data"
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=main")
    (seed / "tracked.txt").write_text("before\n")
    (seed / "delete.txt").write_text("delete\n")
    git(seed, "add", ".")
    identity = dict(
        os.environ,
        GIT_AUTHOR_NAME="seed",
        GIT_AUTHOR_EMAIL="seed@example.test",
        GIT_COMMITTER_NAME="seed",
        GIT_COMMITTER_EMAIL="seed@example.test",
    )
    git(seed, "commit", "-m", "initial", env=identity)
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "main")

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
        VALUES (?,?,21,'M21','Validation','READY',?,?)""",
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
    trusted = TrustedGit(data / "auth")
    repository = data / "repositories" / str(PID) / "repo.git"
    trusted.ensure_bare(repository, str(remote))
    base = trusted.fetch(repository, str(remote), "main")
    branch = "syntra/m21-validation"
    trusted.create_branch(repository, branch, base)
    worktree = data / "workspaces" / str(PID) / str(MID)
    trusted.add_worktree(repository, worktree, branch)
    records = SQLiteWorkspaceRepository(db)
    records.create_managed(
        ManagedRepository(
            "repo", PID, "gh", repository, str(remote), "main", NOW, base
        ),
        NOW,
    )
    records.create_workspace(
        Workspace(
            "workspace",
            PID,
            MID,
            "repo",
            branch,
            worktree,
            "main",
            base,
            None,
            WorkspaceState.READY,
            NOW,
        )
    )
    db.commit()
    service = ChangeValidationService(db, trusted, data)
    yield db, worktree, trusted, service
    db.close()


def test_collects_tracked_added_deleted_staged_untracked_and_binary(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
) -> None:
    _db, worktree, _git, service = validation_workspace
    (worktree / "tracked.txt").write_text("after\n")
    (worktree / "delete.txt").unlink()
    (worktree / "staged.txt").write_text("staged\n")
    git(worktree, "add", "staged.txt")
    (worktree / "untracked.txt").write_text("untracked\n")
    (worktree / "binary.dat").write_bytes(b"\x00\xff\x10")

    result = service.validate(PID, MID, "corr")

    assert result.decision is ValidationDecision.ACCEPT
    assert result.changed_files == ("tracked.txt",)
    assert set(result.added_files) == {"binary.dat", "staged.txt", "untracked.txt"}
    assert result.deleted_files == ("delete.txt",)
    assert result.staged_files == ("staged.txt",)
    assert next(item for item in result.files if item.path == "binary.dat").binary


def test_empty_protected_scope_and_symlink_escape_decisions(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
    tmp_path: Path,
) -> None:
    _db, worktree, _git, service = validation_workspace
    empty = service.validate(PID, MID, "empty")
    assert empty.is_empty and empty.decision is ValidationDecision.REWORK_REQUIRED
    assert empty.findings[0].code is FindingCode.EMPTY_CHANGE_SET

    workflow = worktree / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: safe\n")
    rejected = service.validate(PID, MID, "protected")
    assert rejected.decision is ValidationDecision.REWORK_REQUIRED
    accepted = service.validate(
        PID, MID, "authorised", authorised_protected_paths=(".github/workflows/**",)
    )
    assert accepted.decision is ValidationDecision.ACCEPT
    workflow.unlink()
    workflow.parent.rmdir()
    workflow.parent.parent.rmdir()

    (worktree / "escape").symlink_to(tmp_path / "outside")
    escaped = service.validate(PID, MID, "escape")
    assert escaped.decision is ValidationDecision.BLOCKED
    assert escaped.findings[0].code is FindingCode.WORKSPACE_ESCAPE_ATTEMPT
    assert (worktree / "escape").is_symlink()  # rejection preserves evidence


@pytest.mark.parametrize(
    "payload",
    (
        "token=ghp_SYNTHETIC0123456789ABCDE\n",
        "-----BEGIN PRIVATE KEY-----\nsynthetic-only\n",
        "DATABASE_URL=postgresql://synthetic:only-for-tests@example.test/db\n",
    ),
)
def test_secrets_are_rework_and_never_persisted_or_logged(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
    payload: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db, worktree, _git, service = validation_workspace
    (worktree / "config.env").write_text(payload)
    caplog.set_level(logging.INFO)
    result = service.validate(PID, MID, "secret")
    assert result.decision is ValidationDecision.REWORK_REQUIRED
    assert any(item.code is FindingCode.SECRET_DETECTED for item in result.findings)
    persisted = " ".join(
        str(value)
        for row in db.execute("SELECT * FROM validation_findings")
        for value in row
    )
    assert payload.strip() not in persisted
    assert payload.strip() not in caplog.text


def test_validation_hash_is_bound_to_trusted_commit_and_untracked_bytes(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
) -> None:
    db, worktree, trusted, validation = validation_workspace
    workspace_service = WorkspaceService(db, trusted, worktree.parents[2])
    added = worktree / "new.txt"
    added.write_text("version one\n")
    first = validation.validate(PID, MID, "first")
    assert first.decision is ValidationDecision.ACCEPT
    added.write_text("version two\n")
    with pytest.raises(WorkspaceError, match="changed after validation"):
        workspace_service.commit(
            PID,
            MID,
            first.trusted_head_sha,
            ["new.txt"],
            "unsafe",
            expected_diff_hash=first.diff_hash,
        )
    added.unlink()
    with pytest.raises(WorkspaceError, match="changed after validation"):
        workspace_service.commit(
            PID,
            MID,
            first.trusted_head_sha,
            ["new.txt"],
            "unsafe",
            expected_diff_hash=first.diff_hash,
        )
    added.write_text("final\n")
    final = validation.validate(PID, MID, "final")
    commit = workspace_service.commit(
        PID,
        MID,
        final.trusted_head_sha,
        ["new.txt"],
        "accepted",
        expected_diff_hash=final.diff_hash,
    )
    assert commit.parent_sha == final.trusted_head_sha
    assert (
        db.execute(
            "SELECT validated_diff_hash FROM commits WHERE id=?", (commit.id,)
        ).fetchone()[0]
        == final.diff_hash
    )


def test_rework_validation_is_relative_to_latest_trusted_head(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
) -> None:
    db, worktree, trusted, validation = validation_workspace
    workspace_service = WorkspaceService(db, trusted, worktree.parents[2])
    original_base = trusted.head(worktree)
    (worktree / "file-one.txt").write_text("first cycle\n")

    first = validation.validate(PID, MID, "first-cycle")
    assert first.base_sha == original_base
    assert first.trusted_head_sha == original_base
    commit_b = workspace_service.commit(
        PID,
        MID,
        first.trusted_head_sha,
        ["file-one.txt"],
        "first trusted commit",
        expected_diff_hash=first.diff_hash,
    )
    persisted = SQLiteWorkspaceRepository(db).workspace_for_milestone(MID)
    assert persisted is not None
    assert persisted.base_sha == original_base
    assert persisted.current_head_sha == commit_b.commit_sha

    (worktree / "file-two.txt").write_text("second cycle\n")
    second = validation.validate(PID, MID, "second-cycle")
    assert second.base_sha == original_base
    assert second.trusted_head_sha == commit_b.commit_sha
    assert second.added_files == ("file-two.txt",)
    assert "file-one.txt" not in {item.path for item in second.files}
    commit_c = workspace_service.commit(
        PID,
        MID,
        second.trusted_head_sha,
        ["file-two.txt"],
        "second trusted commit",
        expected_diff_hash=second.diff_hash,
    )
    assert commit_c.parent_sha == commit_b.commit_sha

    for column, value in (
        ("change_set_id", first.id),
        ("validated_diff_hash", first.diff_hash),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                f"UPDATE commits SET {column}=? WHERE id=?", (value, commit_c.id)
            )
        db.rollback()


def test_validation_from_earlier_trusted_head_cannot_be_reused(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
) -> None:
    db, worktree, trusted, validation = validation_workspace
    workspace_service = WorkspaceService(db, trusted, worktree.parents[2])
    (worktree / "file-one.txt").write_text("first\n")
    first = validation.validate(PID, MID, "head-a")
    commit_b = workspace_service.commit(
        PID,
        MID,
        first.trusted_head_sha,
        ["file-one.txt"],
        "advance to B",
        expected_diff_hash=first.diff_hash,
    )

    (worktree / "file-two.txt").write_text("same-looking later diff\n")
    current = ChangeCollector().collect(worktree, commit_b.commit_sha)
    # Persist otherwise matching ACCEPT evidence deliberately anchored to A.
    stale = replace(
        first,
        id=str(uuid4()),
        trusted_head_sha=first.trusted_head_sha,
        files=current.files,
        diff_hash=current.canonical_hash,
        findings=(),
        correlation_id="stale-head-evidence",
    )
    SQLiteValidationRepository(db).save(stale)
    db.commit()

    with pytest.raises(WorkspaceError, match="for trusted HEAD"):
        workspace_service.commit(
            PID,
            MID,
            commit_b.commit_sha,
            ["file-two.txt"],
            "must not use A evidence",
            expected_diff_hash=current.canonical_hash,
        )


@pytest.mark.parametrize(
    "git_type",
    (MutateBeforeStageGit, AlterIndexContentGit, AlterIndexPathsGit),
)
def test_index_verification_refuses_staging_races_without_committing(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
    git_type: type[TrustedGit],
) -> None:
    db, worktree, trusted, validation = validation_workspace
    original_head = trusted.head(worktree)
    (worktree / "race.txt").write_text("content A\n")
    accepted = validation.validate(PID, MID, "staging-race")
    racing_service = WorkspaceService(
        db, git_type(trusted.authentication_root), worktree.parents[2]
    )

    with pytest.raises(WorkspaceError, match="staged index differs"):
        racing_service.commit(
            PID,
            MID,
            accepted.trusted_head_sha,
            ["race.txt"],
            "must not commit raced content",
            expected_diff_hash=accepted.diff_hash,
        )

    assert trusted.head(worktree) == original_head
    assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 0


def test_mutation_after_staging_commits_validated_index_and_leaves_dirty_workspace(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
) -> None:
    db, worktree, trusted, validation = validation_workspace
    (worktree / "race.txt").write_text("content A\n")
    accepted = validation.validate(PID, MID, "post-stage-race")
    racing_service = WorkspaceService(
        db, MutateAfterStageGit(trusted.authentication_root), worktree.parents[2]
    )

    commit = racing_service.commit(
        PID,
        MID,
        accepted.trusted_head_sha,
        ["race.txt"],
        "commit validated index",
        expected_diff_hash=accepted.diff_hash,
    )

    assert git(worktree, "show", f"{commit.commit_sha}:race.txt") == "content A"
    assert (worktree / "race.txt").read_text() == "content B\n"
    persisted = SQLiteWorkspaceRepository(db).workspace_for_milestone(MID)
    assert persisted is not None and persisted.state is WorkspaceState.DIRTY


def test_index_verification_handles_deletion_symlink_and_binary_exactly(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
) -> None:
    db, worktree, trusted, validation = validation_workspace
    (worktree / "delete.txt").unlink()
    (worktree / "link.txt").symlink_to("tracked.txt")
    binary = b"\x00\xffvalidated\x10"
    (worktree / "binary.dat").write_bytes(binary)
    accepted = validation.validate(PID, MID, "index-kinds")
    service = WorkspaceService(db, trusted, worktree.parents[2])

    commit = service.commit(
        PID,
        MID,
        accepted.trusted_head_sha,
        ["binary.dat", "delete.txt", "link.txt"],
        "commit exact index kinds",
        expected_diff_hash=accepted.diff_hash,
    )

    assert git(worktree, "show", f"{commit.commit_sha}:link.txt") == "tracked.txt"
    binary_result = subprocess.run(
        ["git", "show", f"{commit.commit_sha}:binary.dat"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    assert binary_result.stdout == binary
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit.commit_sha}:delete.txt"],
            cwd=worktree,
            check=False,
        ).returncode
        != 0
    )
    persisted = SQLiteWorkspaceRepository(db).workspace_for_milestone(MID)
    assert persisted is not None and persisted.state is WorkspaceState.READY


def test_wrong_remote_branch_and_unexpected_commit_are_blocked(
    validation_workspace: tuple[
        sqlite3.Connection, Path, TrustedGit, ChangeValidationService
    ],
) -> None:
    _db, worktree, _git, service = validation_workspace
    git(worktree, "remote", "set-url", "origin", "/wrong")
    (worktree / "change.txt").write_text("x")
    assert service.validate(PID, MID, "remote").decision is ValidationDecision.BLOCKED
    git(
        worktree, "remote", "set-url", "origin", str(worktree.parents[4] / "remote.git")
    )
    git(worktree, "checkout", "--detach")
    assert service.validate(PID, MID, "branch").decision is ValidationDecision.BLOCKED
