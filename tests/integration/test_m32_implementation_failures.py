# ruff: noqa: E501
"""Regression evidence for the empty implementation / replacement-job incident."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_m32_change_validation_executor import (
    NOW,
    validation_job,
)
from test_m32_change_validation_executor import (
    seed as seed_validation,
)
from test_m32_initial_codex_executor import (
    NOW as CODEX_NOW,
)
from test_m32_initial_codex_executor import (
    executor as codex_executor,
)
from test_m32_initial_codex_executor import (
    job as coding_job,
)
from test_m32_initial_codex_executor import (
    persist_success,
    replay_request,
    state,
)
from test_m32_initial_codex_executor import (
    seed as seed_codex,
)

from syntra_build.application.lifecycle import LifecycleCoordinator
from syntra_build.application.m32_executors import (
    ChangeValidationExecutor,
    InitialCodexExecutor,
)
from syntra_build.application.scheduler import JobExecutionDisposition
from syntra_build.domain import JobId, JobState
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository


@pytest.mark.parametrize(
    "summary",
    [
        "",
        "M1 is blocked by the execution environment. No files were changed and no tests were run.",
        "Implementation complete; all tests pass.",
    ],
)
def test_exit_zero_without_changes_blocks_regardless_of_model_prose(
    tmp_path: Path, summary: str
) -> None:
    database, workspace = tmp_path / "state.db", tmp_path / "worktree"
    db = seed_codex(database, workspace)
    persist_success(db, replay_request(workspace), summary)
    result = codex_executor(database, []).execute(coding_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    assert result.error_id == "codex-empty-implementation"
    assert state(db) == "BLOCKED"
    assert db.execute("SELECT process_status,exit_code FROM codex_runs").fetchone()[
        :
    ] == ("SUCCEEDED", 0)
    for _ in range(3000):
        assert LifecycleCoordinator(db, clock=lambda: CODEX_NOW).enqueue_due() == 0
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM change_sets").fetchone()[0] == 0


@pytest.mark.parametrize("job_type", ["CODEX_RUN", "CHANGE_VALIDATE"])
def test_terminal_job_budget_survives_3000_ticks_and_restart(
    tmp_path: Path, job_type: str
) -> None:
    database, _workspace, _git = seed_validation(tmp_path)
    with open_database(database) as db:
        if job_type == "CODEX_RUN":
            db.execute("UPDATE milestones SET state='CODING'")
        original = validation_job(
            job_type=job_type,
            worker_class=coding_job().worker_class
            if job_type == "CODEX_RUN"
            else validation_job().worker_class,
            state=JobState.FAILED,
            last_error_id="terminal-test",
        )
        SQLiteJobRepository(db, lambda: str(uuid4())).add(original)
        before = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        assert LifecycleCoordinator(db, clock=lambda: NOW).enqueue_due() == 0
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    with open_database(database) as restarted:
        for _ in range(3000):
            assert LifecycleCoordinator(restarted, clock=lambda: NOW).enqueue_due() == 0
        assert restarted.execute("SELECT count(*) FROM jobs").fetchone()[0] == before
        assert restarted.execute("SELECT count(*) FROM change_sets").fetchone()[0] == 0


def test_empty_validation_handoff_is_durable_and_restart_does_not_repeat_evidence(
    tmp_path: Path,
) -> None:
    database, workspace, git = seed_validation(tmp_path)
    executor = ChangeValidationExecutor(
        database, git, tmp_path / "data", clock=lambda: NOW
    )
    result = executor.execute(validation_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    with open_database(database) as db:
        first = db.execute("SELECT id,diff_hash FROM change_sets").fetchone()[:]
        rework = db.execute("SELECT * FROM jobs WHERE state='QUEUED'").fetchone()
        assert json.loads(rework["payload_json"]) == {"validation_id": first[0]}
        assert rework["max_attempts"] == 1
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "CODING"
    # The validation worker may have completed its handoff just before a crash.
    replay = executor.execute(validation_job())
    assert replay.disposition is JobExecutionDisposition.FAILED
    with open_database(database) as restarted:
        for _ in range(3000):
            assert LifecycleCoordinator(restarted, clock=lambda: NOW).enqueue_due() == 0
        assert (
            restarted.execute("SELECT id,diff_hash FROM change_sets").fetchone()[:]
            == first
        )
        assert restarted.execute("SELECT count(*) FROM change_sets").fetchone()[0] == 1
        assert (
            restarted.execute(
                "SELECT count(*) FROM jobs WHERE state='QUEUED'"
            ).fetchone()[0]
            == 1
        )
    assert workspace.is_dir()


def test_cycle_limit_blocks_without_resetting_historical_invocations(
    tmp_path: Path,
) -> None:
    database, workspace, git = seed_validation(tmp_path)
    result = ChangeValidationExecutor(
        database, git, tmp_path / "data", cycle_limit=1, clock=lambda: NOW
    ).execute(validation_job())
    assert result.disposition is JobExecutionDisposition.FAILED
    with open_database(database) as db:
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
        assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
        assert db.execute("SELECT count(*) FROM codex_runs").fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM jobs WHERE state='QUEUED'").fetchone()[0]
            == 0
        )
        assert (
            "exhausted"
            in db.execute(
                "SELECT reason FROM state_transitions WHERE new_state='BLOCKED' LIMIT 1"
            ).fetchone()[0]
        )
    assert workspace.is_dir()


def prepared_application(tmp_path: Path) -> tuple[Path, Path, TrustedGit]:
    from collections.abc import Mapping
    from typing import cast

    from test_m32_one_milestone_happy_path import (
        FakeActions,
        FakeArchitect,
        FakeCodexProcess,
        FakeGitHub,
        config,
        seed,
    )

    from syntra_build.application.architect import ArchitectProvider
    from syntra_build.application.production import (
        ProductionDependencies,
        ProductionDueWorkCoordinator,
        build_production_executors,
    )
    from syntra_build.application.scheduler import (
        JobExecutor,
        Scheduler,
        WorkerCapacity,
    )
    from syntra_build.domain import WorkerClass

    database, data, git = seed(tmp_path)
    dependencies = ProductionDependencies(
        architect_provider_factory=lambda: cast(ArchitectProvider, FakeArchitect()),
        codex_runner_factory=lambda connection: FakeCodexProcess(connection, git, data),
        trusted_git=git,
        github_pull_requests=FakeGitHub(),
        github_actions_factory=FakeActions,
    )
    executors = build_production_executors(
        config(tmp_path, database, data), dependencies=dependencies
    )
    db = open_database(database)
    scheduler = Scheduler(
        SQLiteJobRepository(db, lambda: str(uuid4())),
        WorkerCapacity({worker: 1 for worker in WorkerClass}),
        cast(Mapping[WorkerClass, JobExecutor], executors),
        due_work_enqueuer=ProductionDueWorkCoordinator(db).enqueue_due,
    )
    try:
        for _ in range(40):
            if (
                db.execute("SELECT state FROM milestones").fetchone()[0]
                == "VALIDATING_CHANGES"
            ):
                scheduler.enter_drain()
                if scheduler.active_execution_count:
                    scheduler.wait_for_wake(5)
                scheduler.run_once()
                break
            scheduler.run_once()
            if scheduler.active_execution_count:
                scheduler.wait_for_wake(5)
        else:
            raise AssertionError("application did not reach validation")
    finally:
        scheduler.close()
        db.close()
    return database, data, git


@pytest.mark.parametrize("corrects_findings", [True, False])
def test_real_validation_rework_preserves_uncommitted_work_and_requires_effective_delta(
    tmp_path: Path, corrects_findings: bool
) -> None:

    from test_m32_one_milestone_happy_path import MILESTONE, PROJECT, FakeCodexProcess

    from syntra_build.domain.codex import CodexRunRequest, CodexRunResult

    database, data, git = prepared_application(tmp_path)
    workspace = data / "workspaces" / str(PROJECT) / str(MILESTONE)
    (workspace / "config.env").write_text("api_key='SYNTHETIC_FAKE_VALUE_12345'\n")
    with open_database(database) as db:
        baseline_head = db.execute(
            "SELECT current_head_sha FROM git_workspaces"
        ).fetchone()[0]
        documents = [
            tuple(row)
            for row in db.execute(
                "SELECT id,status,content_hash FROM project_documents"
            )
        ]
    validation = validation_job(
        id=JobId.generate(), project_id=PROJECT, milestone_id=MILESTONE
    )
    result = ChangeValidationExecutor(database, git, data).execute(validation)
    assert result.disposition is JobExecutionDisposition.FAILED
    with open_database(database) as db:
        row = db.execute(
            "SELECT id FROM jobs WHERE job_type='CODEX_RUN' AND state='QUEUED'"
        ).fetchone()
        queued = SQLiteJobRepository(db, lambda: str(uuid4())).get(
            JobId.from_string(row[0]), PROJECT
        )
        assert db.execute("SELECT count(*) FROM codex_runs").fetchone()[0] == 1

    class CorrectingProcess(FakeCodexProcess):
        def run(
            self, request: CodexRunRequest, *, require_clean: bool = True
        ) -> CodexRunResult:
            assert not require_clean
            assert request.previous_run_summary is not None
            assert "SECRET_DETECTED" in request.previous_run_summary
            assert "SYNTHETIC_FAKE_VALUE" not in request.previous_run_summary
            assert (
                request.worktree_path / "generated.py"
            ).read_text() == "answer = 42\n"
            if corrects_findings:
                (request.worktree_path / "config.env").unlink()
            return super().run(request, require_clean=require_clean)

    implementation = InitialCodexExecutor(
        database,
        lambda connection: CorrectingProcess(connection, git, data),
        timeout_seconds=60,
    )
    outcome = implementation.execute(queued)
    assert outcome.disposition is (
        JobExecutionDisposition.SUCCEEDED
        if corrects_findings
        else JobExecutionDisposition.FAILED
    )
    with open_database(database) as db:
        assert (
            db.execute("SELECT current_head_sha FROM git_workspaces").fetchone()[0]
            == baseline_head
        )
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT id,status,content_hash FROM project_documents"
            )
        ] == documents
        assert db.execute("SELECT count(*) FROM codex_runs").fetchone()[0] == 2
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == (
            "VALIDATING_CHANGES" if corrects_findings else "BLOCKED"
        )
    assert (workspace / "generated.py").read_text() == "answer = 42\n"
    if corrects_findings:
        assert (
            ChangeValidationExecutor(database, git, data)
            .execute(
                replace(validation, id=JobId.generate(), correlation_id=str(uuid4()))
            )
            .disposition
            is JobExecutionDisposition.SUCCEEDED
        )


@pytest.mark.parametrize(
    "unsafe", ["none", "nonempty", "wrong-project", "exhausted", "not-paused"]
)
def test_operator_recovery_is_scoped_idempotent_and_preserves_original_history(
    tmp_path: Path, unsafe: str
) -> None:
    from datetime import UTC, datetime

    from test_m32_one_milestone_happy_path import MILESTONE, PROJECT

    from syntra_build.application.change_validation import ChangeValidationService
    from syntra_build.application.implementation_recovery import (
        recover_empty_implementation,
    )
    from syntra_build.domain import ProjectId, ProjectState, ProjectTransitionRequest
    from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

    database, data, git = prepared_application(tmp_path)
    workspace = data / "workspaces" / str(PROJECT) / str(MILESTONE)
    (
        workspace / "generated.py"
    ).unlink()  # Reproduce legacy successful process / empty output.
    with open_database(database) as db:
        ChangeValidationService(db, git, data).validate(
            PROJECT, MILESTONE, "legacy-empty-result"
        )
        if unsafe != "not-paused":
            SQLiteProjectRepository(db, lambda: str(uuid4())).apply_transition(
                ProjectTransitionRequest(
                    PROJECT,
                    ProjectState.BUILDING,
                    ProjectState.PAUSED,
                    "operator pause",
                    "HUMAN",
                    "owner",
                    str(uuid4()),
                    datetime.now(UTC),
                )
            )
        snapshots = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in (
                "project_documents",
                "design_packages",
                "codex_runs",
                "change_sets",
                "validation_findings",
                "git_workspaces",
            )
        }
        if unsafe == "nonempty":
            (workspace / "partial.py").write_text("preserve = True\n")
        target = ProjectId.generate() if unsafe == "wrong-project" else PROJECT
        if unsafe != "none":
            with pytest.raises(Exception):
                recover_empty_implementation(
                    db,
                    git,
                    data,
                    target,
                    MILESTONE,
                    cycle_limit=1 if unsafe == "exhausted" else 5,
                )
        else:
            job_id = recover_empty_implementation(
                db, git, data, PROJECT, MILESTONE, cycle_limit=5
            )
            assert (
                recover_empty_implementation(
                    db, git, data, PROJECT, MILESTONE, cycle_limit=5
                )
                == job_id
            )
            assert db.execute("SELECT state FROM projects").fetchone()[0] == "PAUSED"
            assert db.execute("SELECT state FROM milestones").fetchone()[0] == "CODING"
            assert LifecycleCoordinator(db).enqueue_due() == 0
            queued = SQLiteJobRepository(db, lambda: str(uuid4())).get(job_id, PROJECT)
            assert queued.max_attempts == 1
            assert (queued.payload or {})[
                "operator_recovery"
            ] == "legacy-empty-implementation"
        for table, snapshot in snapshots.items():
            assert [
                tuple(row) for row in db.execute(f"SELECT * FROM {table}")
            ] == snapshot
        assert db.execute("SELECT count(*) FROM codex_runs").fetchone()[0] == 1
    if unsafe == "nonempty":
        assert (workspace / "partial.py").read_text() == "preserve = True\n"


def test_status_uses_empty_completion_blocker_instead_of_stale_git_activity(
    tmp_path: Path,
) -> None:
    from test_m32_initial_codex_executor import PID

    from syntra_build.application.status import SQLiteStatusService
    from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

    database, workspace = tmp_path / "state.db", tmp_path / "worktree"
    db = seed_codex(database, workspace)
    db.execute("UPDATE projects SET activity='Git running'")
    persist_success(db, replay_request(workspace))
    codex_executor(database, []).execute(coding_job())
    status = SQLiteStatusService(
        db, SQLiteProjectRepository(db, lambda: str(uuid4()))
    ).project_status(PID)
    assert "produced no code changes" in status.activity
    assert "Git running" not in status.activity
    assert "blocker" in status.next_action
