"""Provider-independent contracts for trusted M19 Git workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from syntra_build.domain.identifiers import MilestoneId, ProjectId


class WorkspaceState(StrEnum):
    ACTIVE = "ACTIVE"
    DIRTY = "DIRTY"
    READY = "READY"
    ORPHANED = "ORPHANED"
    REMOVED = "REMOVED"
    ERROR = "ERROR"


class WorkspaceError(RuntimeError):
    """A workspace identity or trusted Git invariant failed."""


class PushNotAppliedError(WorkspaceError):
    """An uncertain push was reconciled to an absent or unchanged remote ref."""


class AmbiguousPushError(WorkspaceError):
    """A push failed after it may have changed the remote."""


@dataclass(frozen=True, slots=True)
class ManagedRepository:
    id: str
    project_id: ProjectId
    github_repository_id: str
    path: Path
    remote_url: str
    default_branch: str
    last_fetch_at: datetime | None
    last_known_main_sha: str | None


@dataclass(frozen=True, slots=True)
class Workspace:
    id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    git_repository_id: str
    branch_name: str
    path: Path
    base_branch: str
    base_sha: str
    current_head_sha: str | None
    state: WorkspaceState
    created_at: datetime
    last_validated_at: datetime | None = None
    removed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceInspection:
    workspace: Workspace
    origin_url: str
    current_branch: str
    head_sha: str
    registered: bool
    tracked: tuple[str, ...]
    staged: tuple[str, ...]
    untracked: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (self.tracked or self.staged or self.untracked)


@dataclass(frozen=True, slots=True)
class TrustedCommit:
    id: str
    workspace_id: str
    commit_sha: str
    parent_sha: str
    branch_name: str
    message: str
    created_at: datetime
    pushed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TrustedPushResult:
    branch_name: str
    commit_sha: str
    remote_sha: str
