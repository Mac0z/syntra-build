# ruff: noqa: E501
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIObservation,
    CIOverallStatus,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.infrastructure.persistence.ci import SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
)
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)


def _seed(
    connection: sqlite3.Connection, name: str = "repo"
) -> tuple[ProjectId, MilestoneId, str, str]:
    now = datetime.now(UTC)
    project, milestone, repository = (
        ProjectId.generate(),
        MilestoneId.generate(),
        str(uuid4()),
    )
    external_repository_id = 77 if name in {"repo", "one"} else 78
    connection.execute(
        """INSERT INTO projects
        (id,name,state,resume_state,activity,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility)
        VALUES (?,?,'BUILDING',NULL,NULL,?,?,?,?, 'public')""",
        (str(project), name, now.isoformat(), now.isoformat(), now.isoformat(), name),
    )
    connection.execute(
        """INSERT INTO milestones
        (id,project_id,sequence_number,code,title,state,resume_state,activity,started_at,completed_at,
         created_at,updated_at,codex_cycle_count,ci_rework_count,architect_rework_count,human_test_rework_count,
         exhaustion_reason,active_pull_request_id) VALUES (?,?,23,'M23','CI','CI_RUNNING',NULL,NULL,?,NULL,?,?,0,0,0,0,NULL,NULL)""",
        (
            str(milestone),
            str(project),
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    connection.execute(
        """INSERT INTO github_repositories VALUES
        (?,?,'github','owner',? ,?,?,'public','main','VERIFIED',?,?,?)""",
        (
            repository,
            str(project),
            name,
            f"owner/{name}",
            external_repository_id,
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        external_repository_id,
        12,
        PullRequestState.OPEN,
        "syntra/m23",
        "main",
        "a" * 40,
        "https://example/pr/12",
    )
    with transaction(connection):
        pr = SQLitePullRequestRepository(connection).save_verified(
            str(uuid4()), repository, descriptor, "M23", now
        )
    return project, milestone, repository, pr.id


def test_migration_019_is_contiguous_and_upgrades_018(tmp_path: Path) -> None:
    assert MIGRATIONS[-1].version == 19 and MIGRATIONS[-1].name == "019_ci_monitoring"
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection, MIGRATIONS[:-1])
        apply_migrations(connection)
        assert current_schema_version(connection) == 19


def test_ci_runs_are_exact_sha_historical_and_checks_are_idempotent(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, _, pr_id = _seed(connection)
        records = SQLiteCIRepository(connection)
        check = CICheck("validate", "job-1", CICheckStatus.RUNNING)
        now = datetime.now(UTC)
        with transaction(connection):
            first = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.RUNNING,
                (check,),
                ("run-1",),
                None,
                now,
            )
        passed = CICheck(
            "validate", "job-1", CICheckStatus.COMPLETED, CICheckConclusion.PASSED
        )
        with transaction(connection):
            again = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "a" * 40,
                CIOverallStatus.PASSED,
                (passed,),
                ("run-1",),
                None,
                now,
            )
            second = records.reconcile(
                str(project),
                str(milestone),
                pr_id,
                "b" * 40,
                CIOverallStatus.UNKNOWN,
                (),
                (),
                None,
                now,
            )
        assert first.id == again.id and second.id != first.id
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM ci_checks").fetchone()[0] == 1
        assert records.get(first.id).overall_status is CIOverallStatus.PASSED


def test_ci_identity_rejects_cross_project_relationship(tmp_path: Path) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, _, pr_id = _seed(connection, "one")
        other_project, other_milestone, _, _ = _seed(connection, "two")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO ci_runs
                (id,project_id,milestone_id,pull_request_id,head_sha,overall_status,started_at,last_checked_at,summary_json)
                VALUES (?,?,?,?,?,'UNKNOWN',?,?,'{}')""",
                (
                    str(uuid4()),
                    str(other_project),
                    str(milestone),
                    pr_id,
                    "a" * 40,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        assert project != other_project and milestone != other_milestone


def test_monitor_reconciles_new_head_idempotently_and_advances_only_fresh_pass(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "db") as connection:
        apply_migrations(connection)
        project, milestone, _, _ = _seed(connection)

        class PullRequests:
            head = "a" * 40

            def get(
                self,
                repository_full_name: str,
                number: int,
                project_id: ProjectId,
                milestone_id: MilestoneId,
            ) -> PullRequestDescriptor:
                return PullRequestDescriptor(
                    "1.0",
                    project_id,
                    milestone_id,
                    77,
                    number,
                    PullRequestState.OPEN,
                    "syntra/m23",
                    "main",
                    self.head,
                    "https://example/pr/12",
                )

            def find_open(self, *args: object) -> tuple[()]:
                return ()

            def create(self, *args: object) -> PullRequestDescriptor:
                raise AssertionError("CI monitoring is read-only")

        class Actions:
            passing = False

            def observe(
                self, repository_full_name: str, head_sha: str
            ) -> CIObservation:
                check = (
                    CICheck(
                        "validate",
                        "job-1",
                        CICheckStatus.COMPLETED,
                        CICheckConclusion.PASSED,
                    )
                    if self.passing
                    else CICheck("validate", "job-1", CICheckStatus.RUNNING)
                )
                return CIObservation((check,), ("run-1",))

        prs, actions = PullRequests(), Actions()
        monitor = CIMonitor(connection, prs, actions)
        first = monitor.reconcile(project, milestone, "correlation")
        repeated = monitor.reconcile(project, milestone, "correlation")
        assert first.id == repeated.id
        prs.head = "b" * 40
        actions.passing = True
        fresh = monitor.reconcile(project, milestone, "correlation")
        assert fresh.id != first.id and fresh.head_sha == "b" * 40
        assert (
            connection.execute(
                "SELECT state FROM milestones WHERE id=?", (str(milestone),)
            ).fetchone()[0]
            == "ARCHITECT_REVIEW"
        )
        assert connection.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 2
