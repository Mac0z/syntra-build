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
