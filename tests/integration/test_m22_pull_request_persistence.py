# ruff: noqa: E501
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    apply_migrations,
)
from syntra_build.infrastructure.persistence.pull_requests import (
    PullRequestPersistenceConflict,
    SQLitePullRequestRepository,
)


def _seed(connection: sqlite3.Connection) -> tuple[ProjectId, MilestoneId, str]:
    now = datetime.now(UTC).isoformat()
    project, milestone, repository = (
        ProjectId.generate(),
        MilestoneId.generate(),
        str(uuid4()),
    )
    connection.execute(
        """INSERT INTO projects
        (id,name,state,resume_state,activity,created_at,updated_at,last_state_change_at,
         canonical_name,repository_visibility) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (str(project), "M22", "BUILDING", None, None, now, now, now, "repo", "public"),
    )
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,resume_state,activity,
         started_at,completed_at,created_at,updated_at,codex_cycle_count,ci_rework_count,
         architect_rework_count,human_test_rework_count,exhaustion_reason,active_pull_request_id)
        VALUES (?,?,?,?,?,'PR_CREATING',NULL,NULL,?,NULL,?,?,0,0,0,0,NULL,NULL)""",
        (str(milestone), str(project), 22, "M22", "Pull requests", now, now, now),
    )
    connection.execute(
        """INSERT INTO github_repositories VALUES
        (?,?, 'github','owner','repo','owner/repo',77,'public','main','VERIFIED',?,?,?)""",
        (repository, str(project), now, now, now),
    )
    return project, milestone, repository


def test_migration_018_applies_clean_and_from_017(tmp_path: Path) -> None:
    for migrations in (MIGRATIONS, MIGRATIONS[:-1]):
        with open_database(tmp_path / f"db-{len(migrations)}.sqlite") as connection:
            apply_migrations(connection, migrations)
            apply_migrations(connection)
            assert connection.execute("PRAGMA user_version").fetchone() is not None


def test_pr_identity_constraints_and_mutable_reconciliation(tmp_path: Path) -> None:
    with open_database(tmp_path / "db.sqlite") as connection:
        apply_migrations(connection)
        project, milestone, repository = _seed(connection)
        records = SQLitePullRequestRepository(connection)
        descriptor = PullRequestDescriptor(
            "1.0",
            project,
            milestone,
            77,
            12,
            PullRequestState.OPEN,
            "syntra/m22",
            "main",
            "a" * 40,
            "https://github.com/owner/repo/pull/12",
        )
        with transaction(connection):
            first = records.save_verified(
                str(uuid4()), repository, descriptor, "M22", datetime.now(UTC)
            )
        updated = PullRequestDescriptor(
            "1.0",
            project,
            milestone,
            77,
            12,
            PullRequestState.OPEN,
            "syntra/m22",
            "main",
            "b" * 40,
            "https://github.com/owner/repo/pull/12",
        )
        with transaction(connection):
            second = records.save_verified(
                str(uuid4()), repository, updated, "M22", datetime.now(UTC)
            )
        assert (first.id, second.id, second.head_sha) == (first.id, first.id, "b" * 40)
        active = connection.execute(
            "SELECT active_pull_request_id FROM milestones WHERE id=?",
            (str(milestone),),
        ).fetchone()[0]
        assert active == first.id
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE pull_requests SET head_branch='other' WHERE id=?", (first.id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO pull_requests
                SELECT ?,project_id,milestone_id,github_repository_id,13,state,head_branch,
                base_branch,head_sha,web_url,title,created_at,updated_at,merged_at,
                merge_commit_sha,closed_at,last_reconciled_at FROM pull_requests WHERE id=?""",
                (str(uuid4()), first.id),
            )


def test_change_set_commit_and_intent_identity_are_unique(tmp_path: Path) -> None:
    with open_database(tmp_path / "constraints.sqlite") as connection:
        apply_migrations(connection)
        project, milestone, repository = _seed(connection)
        now = datetime.now(UTC).isoformat()
        git_repository, workspace, change_set = (str(uuid4()) for _ in range(3))
        connection.execute(
            """INSERT INTO git_repositories
            (id,project_id,github_repository_id,repository_path,remote_name,remote_url,
             default_branch,last_known_main_sha,created_at,updated_at)
            VALUES (?,?,?,?,'origin',?,'main',?,?,?)""",
            (
                git_repository,
                str(project),
                repository,
                "/tmp/repo.git",
                "https://github.com/owner/repo.git",
                "a" * 40,
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO git_workspaces
            (id,project_id,milestone_id,git_repository_id,branch_name,worktree_path,
             base_branch,base_sha,current_head_sha,state,created_at)
            VALUES (?,?,?,?,?,?,'main',?,?,'READY',?)""",
            (
                workspace,
                str(project),
                str(milestone),
                git_repository,
                "syntra/m22",
                "/tmp/worktree",
                "a" * 40,
                "b" * 40,
                now,
            ),
        )
        diff_hash = "sha256:" + "1" * 64
        connection.execute(
            """INSERT INTO change_sets
            (id,interface_version,project_id,milestone_id,worktree_id,branch_name,
             base_sha,head_sha_before_commit,diff_hash,is_empty,files_json,decision,
             correlation_id,scanner_version,policy_version,created_at)
            VALUES (?,'1.0',?,?,?,?,?,?,?,0,'[]','ACCEPT','c','s','p',?)""",
            (
                change_set,
                str(project),
                str(milestone),
                workspace,
                "syntra/m22",
                "a" * 40,
                "a" * 40,
                diff_hash,
                now,
            ),
        )

        def insert_commit(identifier: str, sha: str) -> None:
            connection.execute(
                """INSERT INTO commits
                (id,project_id,milestone_id,worktree_id,commit_sha,parent_sha,
                 branch_name,message,author_name,author_email,created_at,change_set_id,
                 validated_diff_hash) VALUES (?,?,?,?,?,?,?,'message','Syntra Build',
                 'syntra@localhost',?,?,?)""",
                (
                    identifier,
                    str(project),
                    str(milestone),
                    workspace,
                    sha,
                    "a" * 40,
                    "syntra/m22",
                    now,
                    change_set,
                    diff_hash,
                ),
            )

        insert_commit(str(uuid4()), "b" * 40)
        with pytest.raises(sqlite3.IntegrityError):
            insert_commit(str(uuid4()), "c" * 40)

        from syntra_build.domain.pull_requests import PullRequestCreateRequest

        records = SQLitePullRequestRepository(connection)
        request = PullRequestCreateRequest(
            "1.0",
            "c",
            project,
            milestone,
            77,
            "syntra/m22",
            "main",
            "b" * 40,
            "title",
            "body",
        )
        with transaction(connection):
            records.ensure_intent(
                str(uuid4()), repository, request, "2" * 64, datetime.now(UTC)
            )
        wrong = PullRequestCreateRequest(
            "1.0",
            "c",
            project,
            milestone,
            77,
            "other",
            "main",
            "b" * 40,
            "title",
            "body",
        )
        with pytest.raises(PullRequestPersistenceConflict):
            with transaction(connection):
                records.ensure_intent(
                    str(uuid4()), repository, wrong, "2" * 64, datetime.now(UTC)
                )
