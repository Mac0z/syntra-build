from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from syntra_build.application.provisioning import AmbiguousGitHubResult
from syntra_build.application.pull_requests import (
    GitHubPullRequestGateway,
    PullRequestError,
    PullRequestFailure,
    PullRequestLifecycleService,
)
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.pull_requests import (
    PullRequestCreateRequest,
    PullRequestDescriptor,
    PullRequestState,
)
from syntra_build.domain.workspaces import (
    TrustedCommit,
    TrustedPushResult,
    Workspace,
    WorkspaceInspection,
    WorkspaceState,
)
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.migrations import apply_migrations

NOW = datetime(2026, 1, 2, tzinfo=UTC)
PARENT = "a" * 40
COMMIT = "b" * 40
DIFF = "sha256:" + "1" * 64


class FakeWorkspace:
    def __init__(
        self,
        connection: sqlite3.Connection,
        project: ProjectId,
        milestone: MilestoneId,
        workspace_id: str,
    ) -> None:
        self.connection, self.project, self.milestone = connection, project, milestone
        self.workspace_id = workspace_id
        self.commit_calls = 0
        self.push_calls = 0
        self.remote_mismatch = False

    def commit(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        expected_head: str,
        paths: list[str],
        message: str,
        now: datetime,
        *,
        expected_diff_hash: str | None = None,
    ) -> TrustedCommit:
        self.commit_calls += 1
        identifier = str(uuid4())
        self.connection.execute(
            """INSERT INTO commits
            (id,project_id,milestone_id,worktree_id,commit_sha,parent_sha,branch_name,
             message,author_name,author_email,created_at,change_set_id,validated_diff_hash)
            VALUES (?,?,?,?,?,?,? ,?,'Syntra Build','syntra@localhost',?,?,?)""",
            (
                identifier,
                str(project_id),
                str(milestone_id),
                self.workspace_id,
                COMMIT,
                expected_head,
                "syntra/m22",
                message,
                now.isoformat(),
                CHANGE_SET,
                expected_diff_hash,
            ),
        )
        self.connection.execute(
            "UPDATE git_workspaces SET current_head_sha=? WHERE id=?",
            (COMMIT, self.workspace_id),
        )
        return TrustedCommit(
            identifier,
            self.workspace_id,
            COMMIT,
            expected_head,
            "syntra/m22",
            message,
            now,
        )

    def inspect(
        self, project_id: ProjectId, milestone_id: MilestoneId, now: datetime
    ) -> WorkspaceInspection:
        row = self.connection.execute(
            "SELECT * FROM git_workspaces WHERE id=?", (self.workspace_id,)
        ).fetchone()
        workspace = Workspace(
            row["id"],
            project_id,
            milestone_id,
            row["git_repository_id"],
            row["branch_name"],
            Path(row["worktree_path"]),
            row["base_branch"],
            row["base_sha"],
            row["current_head_sha"],
            WorkspaceState(row["state"]),
            NOW,
        )
        return WorkspaceInspection(
            workspace,
            "https://github.com/owner/repo.git",
            row["branch_name"],
            row["current_head_sha"] or row["base_sha"],
            True,
            (),
            (),
            (),
        )

    def push(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        expected_commit_sha: str,
        now: datetime,
    ) -> TrustedPushResult:
        self.push_calls += 1
        remote = "c" * 40 if self.remote_mismatch else expected_commit_sha
        return TrustedPushResult("syntra/m22", expected_commit_sha, remote)


class FakeGitHub:
    def __init__(
        self, connection: sqlite3.Connection, project: ProjectId, milestone: MilestoneId
    ) -> None:
        self.connection, self.project, self.milestone = connection, project, milestone
        self.remote: list[PullRequestDescriptor] = []
        self.create_calls = 0
        self.get_calls = 0
        self.ambiguous_creates = 0
        self.intent_seen = False

    def find_open(
        self,
        repository_full_name: str,
        head_branch: str,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> tuple[PullRequestDescriptor, ...]:
        return tuple(
            item for item in self.remote if item.state is PullRequestState.OPEN
        )

    def create(
        self, repository_full_name: str, request: PullRequestCreateRequest
    ) -> PullRequestDescriptor:
        self.create_calls += 1
        intent = self.connection.execute(
            "SELECT * FROM pull_request_creation_intents WHERE milestone_id=?",
            (str(request.milestone_id),),
        ).fetchone()
        self.intent_seen = intent is not None and intent["head_sha"] == request.head_sha
        if self.ambiguous_creates:
            self.ambiguous_creates -= 1
            raise AmbiguousGitHubResult("ambiguous")
        descriptor = descriptor_for(request, 12)
        self.remote = [descriptor]
        return descriptor

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        self.get_calls += 1
        return next(item for item in self.remote if item.pull_request_number == number)


def descriptor_for(
    request: PullRequestCreateRequest,
    number: int,
    *,
    state: PullRequestState = PullRequestState.OPEN,
    base: str | None = None,
) -> PullRequestDescriptor:
    return PullRequestDescriptor(
        "1.0",
        request.project_id,
        request.milestone_id,
        77,
        number,
        state,
        request.head_branch,
        base or request.base_branch,
        request.head_sha,
        f"https://github.com/owner/repo/pull/{number}",
    )


CHANGE_SET = "00000000-0000-0000-0000-000000000022"


def setup(connection: sqlite3.Connection) -> tuple[ProjectId, MilestoneId, str]:
    project, milestone = ProjectId.generate(), MilestoneId.generate()
    now = NOW.isoformat()
    repository, git_repository, workspace = (str(uuid4()) for _ in range(3))
    connection.execute(
        """INSERT INTO projects
        (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility)
        VALUES (?,?,'BUILDING',?,?,?,?, 'public')""",
        (str(project), "M22", now, now, now, "repo"),
    )
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,created_at,updated_at)
        VALUES (?,?,22,'M22','Pull requests','PR_CREATING',?,?)""",
        (str(milestone), str(project), now, now),
    )
    connection.execute(
        """INSERT INTO github_repositories VALUES
        (?,?,'github','owner','repo','owner/repo',77,'public','main','VERIFIED',?,?,?)""",
        (repository, str(project), now, now, now),
    )
    connection.execute(
        """INSERT INTO git_repositories
        (id,project_id,github_repository_id,repository_path,remote_name,remote_url,
         default_branch,last_known_main_sha,created_at,updated_at)
        VALUES (?,?,?,'/tmp/repo.git','origin','https://github.com/owner/repo.git',
                'main',?,?,?)""",
        (git_repository, str(project), repository, PARENT, now, now),
    )
    connection.execute(
        """INSERT INTO git_workspaces
        (id,project_id,milestone_id,git_repository_id,branch_name,worktree_path,
         base_branch,base_sha,state,created_at)
        VALUES (?,?,?,?,?,'/tmp/worktree','main',?,'READY',?)""",
        (
            workspace,
            str(project),
            str(milestone),
            git_repository,
            "syntra/m22",
            PARENT,
            now,
        ),
    )
    files = json.dumps([{"path": "file.txt"}])
    connection.execute(
        """INSERT INTO change_sets VALUES
        (?,'1.0',?,?,?,?,?,?,?,0,?,'ACCEPT','correlation','scanner','policy',?)""",
        (
            CHANGE_SET,
            str(project),
            str(milestone),
            workspace,
            "syntra/m22",
            PARENT,
            PARENT,
            DIFF,
            files,
            now,
        ),
    )
    return project, milestone, workspace


def service(
    connection: sqlite3.Connection,
) -> tuple[
    PullRequestLifecycleService, FakeWorkspace, FakeGitHub, ProjectId, MilestoneId
]:
    project, milestone, workspace_id = setup(connection)
    workspaces = FakeWorkspace(connection, project, milestone, workspace_id)
    github = FakeGitHub(connection, project, milestone)
    lifecycle = PullRequestLifecycleService(
        connection,
        cast(WorkspaceService, workspaces),
        cast(GitHubPullRequestGateway, github),
    )
    return lifecycle, workspaces, github, project, milestone


def test_initial_lifecycle_persists_intent_before_create_and_recovers_commit(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        lifecycle, workspaces, github, project, milestone = service(connection)
        first = lifecycle.establish(
            project, milestone, CHANGE_SET, "correlation", now=NOW
        )
        assert github.intent_seen and github.create_calls == 1 and github.get_calls == 1
        assert first.external_pr_number == 12 and first.head_sha == COMMIT
        # Retry converges on the durable commit and existing PR without recommitting.
        second = lifecycle.establish(project, milestone, CHANGE_SET, "retry", now=NOW)
        assert second.id == first.id
        assert workspaces.commit_calls == 1 and github.create_calls == 1


def test_ambiguous_create_reconciles_and_bounded_retry(tmp_path: Path) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        lifecycle, _, github, project, milestone = service(connection)
        github.ambiguous_creates = 1
        result = lifecycle.establish(project, milestone, CHANGE_SET, "c", now=NOW)
        assert result.external_pr_number == 12 and github.create_calls == 2
        status = connection.execute(
            "SELECT status FROM pull_request_creation_intents"
        ).fetchone()[0]
        assert status == "VERIFIED"


def test_second_ambiguous_create_stops_after_bounded_retry(tmp_path: Path) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        lifecycle, _, github, project, milestone = service(connection)
        github.ambiguous_creates = 2
        with pytest.raises(PullRequestError) as raised:
            lifecycle.establish(project, milestone, CHANGE_SET, "c", now=NOW)
        assert raised.value.failure is PullRequestFailure.AMBIGUOUS
        assert github.create_calls == 2
        assert (
            connection.execute(
                "SELECT status FROM pull_request_creation_intents"
            ).fetchone()[0]
            == "AMBIGUOUS"
        )


def test_collision_and_remote_mismatch_fail_before_create(tmp_path: Path) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        lifecycle, workspaces, github, project, milestone = service(connection)
        workspaces.remote_mismatch = True
        with pytest.raises(PullRequestError) as mismatch:
            lifecycle.establish(project, milestone, CHANGE_SET, "c", now=NOW)
        assert mismatch.value.failure is PullRequestFailure.HEAD_SHA_MISMATCH
        assert github.create_calls == 0


def test_multiple_or_conflicting_remote_prs_fail_closed(tmp_path: Path) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        lifecycle, _, github, project, milestone = service(connection)
        request = PullRequestCreateRequest(
            "1.0",
            "c",
            project,
            milestone,
            77,
            "syntra/m22",
            "main",
            COMMIT,
            "title",
            "body",
        )
        github.remote = [descriptor_for(request, 11), descriptor_for(request, 12)]
        with pytest.raises(PullRequestError) as collision:
            lifecycle.establish(project, milestone, CHANGE_SET, "c", now=NOW)
        assert collision.value.failure is PullRequestFailure.PR_COLLISION
        assert github.create_calls == 0
        assert (
            connection.execute(
                "SELECT status FROM pull_request_creation_intents"
            ).fetchone()[0]
            == "BLOCKED"
        )


@pytest.mark.parametrize(
    ("state", "failure"),
    [
        (PullRequestState.CLOSED, PullRequestFailure.PR_CLOSED),
        (PullRequestState.MERGED, PullRequestFailure.PR_MERGED_UNEXPECTEDLY),
    ],
)
def test_terminal_pr_is_observed_preserved_and_not_replaced(
    tmp_path: Path, state: PullRequestState, failure: PullRequestFailure
) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        lifecycle, _, github, project, milestone = service(connection)
        first = lifecycle.establish(project, milestone, CHANGE_SET, "c", now=NOW)
        live = github.remote[0]
        github.remote = [
            PullRequestDescriptor(
                live.interface_version,
                live.project_id,
                live.milestone_id,
                live.repository_id,
                live.pull_request_number,
                state,
                live.head_branch,
                live.base_branch,
                live.head_sha,
                live.web_url,
            )
        ]
        with pytest.raises(PullRequestError) as raised:
            lifecycle.establish(project, milestone, CHANGE_SET, "retry", now=NOW)
        assert raised.value.failure is failure
        assert github.create_calls == 1
        persisted = connection.execute(
            "SELECT id,state FROM pull_requests WHERE milestone_id=?",
            (str(milestone),),
        ).fetchone()
        assert persisted["id"] == first.id and persisted["state"] == state
