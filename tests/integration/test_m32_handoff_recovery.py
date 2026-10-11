"""Recover the successful process / failed implementation handoff incident."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from test_m32_implementation_failures import prepared_application
from test_m32_one_milestone_happy_path import MILESTONE, PROJECT

from syntra_build.application.implementation_recovery import (
    recover_successful_implementation_handoff,
)
from syntra_build.application.lifecycle import LifecycleCoordinator
from syntra_build.application.security import SecurityPolicy
from syntra_build.domain import JobId, ProjectState, ProjectTransitionRequest
from syntra_build.domain.codex import CodexProcessStatus, CodexRunResult
from syntra_build.domain.security import SecurityEventType, SecuritySeverity
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import open_database
from syntra_build.infrastructure.persistence.codex import SQLiteCodexRunRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository


def blocked_application(
    tmp_path: Path, resume: str = "CODING", unsafe_run: str | None = None
) -> tuple[Path, Path, TrustedGit, JobId]:
    complete = SQLiteCodexRunRepository.complete

    def complete_fixture(
        records: SQLiteCodexRunRepository, run_id: str, result: CodexRunResult
    ) -> None:
        if unsafe_run == "failed-run":
            result = replace(result, process_status=CodexProcessStatus.FAILED)
        elif unsafe_run == "nonzero":
            result = replace(result, exit_code=7)
        elif unsafe_run == "wrong-correlation":
            records.connection.execute(
                "UPDATE codex_runs SET correlation_id='wrong' WHERE id=?", (run_id,)
            )
        elif unsafe_run == "wrong-task":
            records.connection.execute(
                "UPDATE codex_runs SET task_content_hash='wrong' WHERE id=?", (run_id,)
            )
        elif unsafe_run == "unterminated":
            return
        complete(records, run_id, result)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SQLiteCodexRunRepository, "complete", complete_fixture)
        database, data, git = prepared_application(tmp_path, handoff_failure=resume)
    with open_database(database) as db:
        job_id = JobId.from_string(
            db.execute("SELECT job_id FROM codex_runs").fetchone()[0]
        )
        assert (
            db.execute("SELECT state FROM jobs WHERE id=?", (str(job_id),)).fetchone()[
                0
            ]
            == "FAILED"
        )
        assert db.execute("SELECT resume_state FROM milestones").fetchone()[0] == resume
    return database, data, git, job_id


def recover(db: sqlite3.Connection, git: TrustedGit, data: Path, job: JobId) -> str:
    return recover_successful_implementation_handoff(
        db,
        git,
        data,
        PROJECT,
        MILESTONE,
        job,
        repair_command=lambda path: ("/bin/true", str(path)),
    )


@pytest.mark.parametrize("resume", ["CODING", "VALIDATING_CHANGES"])
def test_recovery_preserves_evidence_budgets_and_one_authorised_continuation(
    tmp_path: Path, resume: str
) -> None:
    database, data, git, job = blocked_application(tmp_path, resume)
    workspace = data / "workspaces" / str(PROJECT) / str(MILESTONE)
    original = (workspace / "generated.py").read_bytes()
    head = git.head(workspace)
    with open_database(database) as db:
        snapshots = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in (
                "codex_runs",
                "jobs",
                "job_attempts",
                "project_documents",
                "git_workspaces",
            )
        }
        old_history = [
            tuple(row)
            for row in db.execute("SELECT * FROM state_transitions ORDER BY rowid")
        ]
        accepted = recover(db, git, data, job)
        assert recover(db, git, data, job) == accepted
        assert db.execute("SELECT state,resume_state FROM projects").fetchone()[:] == (
            "PAUSED",
            "BUILDING",
        )
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "COMMITTING"
        assert db.execute("SELECT count(*) FROM change_sets").fetchone()[0] == 1
        assert [
            tuple(row)
            for row in db.execute("SELECT * FROM state_transitions ORDER BY rowid")
        ][: len(old_history)] == old_history
        for table, snapshot in snapshots.items():
            assert [
                tuple(row) for row in db.execute(f"SELECT * FROM {table}")
            ] == snapshot
    with open_database(database) as db:
        assert recover(db, git, data, job) == accepted
        for _ in range(3000):
            assert LifecycleCoordinator(db).enqueue_due() == 0
        assert (workspace / "generated.py").read_bytes() == original
        assert git.head(workspace) == head
        # A separate explicit authorisation is simulated only in this fixture.
        SQLiteProjectRepository(db, lambda: str(uuid4())).apply_transition(
            ProjectTransitionRequest(
                PROJECT,
                ProjectState.PAUSED,
                ProjectState.BUILDING,
                "operator authorised resume",
                "HUMAN",
                "owner",
                str(uuid4()),
                datetime.now(UTC),
            )
        )
        assert LifecycleCoordinator(db).enqueue_due() == 1
    with open_database(database) as db:
        for _ in range(3000):
            assert LifecycleCoordinator(db).enqueue_due() == 0
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE job_type='GIT_COMMIT'"
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT count(*) FROM codex_runs").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE job_type='CODEX_RUN'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "unsafe",
    [
        "failed-run",
        "nonzero",
        "unterminated",
        "wrong-attempt",
        "wrong-correlation",
        "wrong-worktree",
        "wrong-task",
        "active-job",
        "security",
        "wrong-resume",
        "wrong-job-type",
        "wrong-blocker",
        "branch",
        "head",
        "empty",
        "secret",
        "protected",
        "escape",
    ],
)
def test_recovery_rejects_inapplicable_or_unsafe_evidence(
    tmp_path: Path, unsafe: str
) -> None:
    run_problems = {
        "failed-run",
        "nonzero",
        "unterminated",
        "wrong-correlation",
        "wrong-task",
    }
    database, data, git, job = blocked_application(
        tmp_path, unsafe_run=unsafe if unsafe in run_problems else None
    )
    workspace = data / "workspaces" / str(PROJECT) / str(MILESTONE)
    with open_database(database) as db:
        mutations = {
            "wrong-attempt": "UPDATE jobs SET attempt_number=0",
            "active-job": "UPDATE jobs SET state='RUNNING' WHERE job_type='CODEX_RUN'",
            "wrong-resume": "UPDATE milestones SET resume_state='COMMITTING'",
            "wrong-job-type": (
                "UPDATE jobs SET job_type='CODEX_REVIEW_REWORK' "
                "WHERE job_type='CODEX_RUN'"
            ),
            "wrong-blocker": (
                "UPDATE jobs SET last_error_id='codex-empty-implementation' "
                "WHERE job_type='CODEX_RUN'"
            ),
        }
        if unsafe in run_problems:
            pass
        elif unsafe in mutations:
            db.execute(mutations[unsafe])
        elif unsafe == "security":
            SecurityPolicy(db).record(
                event_type=SecurityEventType.SECRET_DETECTED,
                severity=SecuritySeverity.HIGH,
                project_id=PROJECT,
                milestone_id=MILESTONE,
                correlation_id="fixture",
                source_reference="fixture",
                blocking=True,
                source_component="test",
                created_at=datetime.now(UTC),
                safe_details={"reason": "fixture blocker"},
            )
        elif unsafe == "wrong-worktree":
            moved = workspace.with_name("displaced")
            workspace.rename(moved)
            workspace.symlink_to(moved, target_is_directory=True)
        elif unsafe == "branch":
            git._run(workspace, ["switch", "-c", "wrong"])
        elif unsafe == "head":
            git._run(
                workspace,
                [
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "unexpected",
                ],
            )
        elif unsafe == "empty":
            (workspace / "generated.py").unlink()
        elif unsafe == "secret":
            (workspace / "config.env").write_text(
                "api_key='SYNTHETIC_FAKE_VALUE_12345'\n"
            )
        elif unsafe == "protected":
            (workspace / "scripts/host").mkdir(parents=True)
            (workspace / "scripts/host/unsafe.sh").write_text("echo unsafe\n")
        elif unsafe == "escape":
            (workspace / "escape").symlink_to(tmp_path / "outside")
        jobs_before = [tuple(row) for row in db.execute("SELECT * FROM jobs")]
        runs_before = [tuple(row) for row in db.execute("SELECT * FROM codex_runs")]
        with pytest.raises((ValueError, RuntimeError)):
            recover(db, git, data, job)
        assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
        assert [tuple(row) for row in db.execute("SELECT * FROM jobs")] == jobs_before
        assert [
            tuple(row) for row in db.execute("SELECT * FROM codex_runs")
        ] == runs_before


def test_repair_failure_and_changed_idempotent_diff_leave_project_non_dispatching(
    tmp_path: Path,
) -> None:
    database, data, git, job = blocked_application(tmp_path)
    with open_database(database) as db:
        with pytest.raises(ValueError, match="repair failed"):
            recover_successful_implementation_handoff(
                db,
                git,
                data,
                PROJECT,
                MILESTONE,
                job,
                repair_command=lambda path: ("/bin/false", str(path)),
            )
        assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
        assert db.execute("SELECT count(*) FROM change_sets").fetchone()[0] == 0
        recover(db, git, data, job)
        workspace = data / "workspaces" / str(PROJECT) / str(MILESTONE)
        (workspace / "generated.py").write_text("different = True\n")
        with pytest.raises(ValueError, match="no longer matches"):
            recover(db, git, data, job)
        assert LifecycleCoordinator(db).enqueue_due() == 0
        marker = db.execute(
            "SELECT metadata_json FROM state_transitions WHERE new_state='COMMITTING'"
        ).fetchone()[0]
        assert (
            json.loads(marker)["operator_recovery"]
            == "successful-implementation-handoff"
        )


def test_interrupted_transition_reuses_validation_without_partial_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, data, git, job = blocked_application(tmp_path)
    transition = SQLiteProjectRepository.apply_transition

    def interrupt(
        repository: SQLiteProjectRepository, request: ProjectTransitionRequest
    ) -> object:
        if request.target_state is ProjectState.PAUSED:
            raise RuntimeError("interrupted recovery")
        return transition(repository, request)

    with open_database(database) as db:
        with monkeypatch.context() as patch:
            patch.setattr(SQLiteProjectRepository, "apply_transition", interrupt)
            with pytest.raises(RuntimeError, match="interrupted recovery"):
                recover(db, git, data, job)
        assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
        assert db.execute("SELECT count(*) FROM change_sets").fetchone()[0] == 1
        accepted = recover(db, git, data, job)
        assert db.execute("SELECT id FROM change_sets").fetchone()[0] == accepted
        assert db.execute("SELECT count(*) FROM codex_runs").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE job_type='CODEX_RUN'"
            ).fetchone()[0]
            == 1
        )
