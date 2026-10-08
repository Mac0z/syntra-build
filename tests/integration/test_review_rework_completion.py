"""Actual-worktree regressions for review completion and validation correction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from test_m32_controlled_rework_path import (
    MILESTONE,
    PROJECT,
    FakeActions,
    FakeArchitect,
    FakeCodexProcess,
    FakeGitHub,
    LocalRemoteTrustedGit,
    config,
    git,
    seed,
)

from syntra_build.application.architect import ArchitectProvider
from syntra_build.application.lifecycle import LifecycleCoordinator
from syntra_build.application.m32_executors import ChangeValidationExecutor
from syntra_build.application.production import (
    ProductionDependencies,
    ProductionDueWorkCoordinator,
    build_production_executors,
)
from syntra_build.application.review_rework import ReviewReworkCodexExecutor
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutor,
    Scheduler,
    WorkerCapacity,
)
from syntra_build.domain import Job, JobId, WorkerClass
from syntra_build.domain.codex import CodexRunRequest, CodexRunResult
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence import open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository


@dataclass
class ReviewApplication:
    database: Path
    data: Path
    trusted_git: TrustedGit
    requests: list[CodexRunRequest]
    queued: Job

    @property
    def workspace(self) -> Path:
        return self.data / "workspaces" / str(PROJECT) / str(MILESTONE)


def prepared_review(tmp_path: Path) -> ReviewApplication:
    database, data, trusted_git = seed(tmp_path)
    requests: list[CodexRunRequest] = []

    def remote_head() -> str:
        heads = git(
            Path(cast(LocalRemoteTrustedGit, trusted_git).remote),
            "for-each-ref",
            "--format=%(objectname)",
            "refs/heads/syntra/*",
        ).splitlines()
        assert len(heads) == 1
        return heads[0]

    dependencies = ProductionDependencies(
        architect_provider_factory=lambda: cast(ArchitectProvider, FakeArchitect()),
        codex_runner_factory=lambda connection: FakeCodexProcess(
            connection, trusted_git, data, requests
        ),
        trusted_git=trusted_git,
        github_pull_requests=FakeGitHub(remote_head),
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
        for _ in range(100):
            row = db.execute(
                "SELECT id FROM jobs WHERE job_type='CODEX_REVIEW_REWORK' "
                "AND state='QUEUED'"
            ).fetchone()
            if row is not None:
                scheduler.enter_drain()
                if scheduler.active_execution_count:
                    scheduler.wait_for_wake(5)
                scheduler.run_once()
                queued = SQLiteJobRepository(db, lambda: str(uuid4())).get(
                    JobId.from_string(row[0]), PROJECT
                )
                assert (
                    db.execute("SELECT state FROM milestones").fetchone()[0] == "CODING"
                )
                return ReviewApplication(database, data, trusted_git, requests, queued)
            scheduler.run_once()
            if scheduler.active_execution_count:
                scheduler.wait_for_wake(5)
        raise AssertionError("did not reach queued Architect review rework")
    finally:
        scheduler.close()
        db.close()


def executor(
    application: ReviewApplication, *, mode: str = "valid", cycle_limit: int = 5
) -> ReviewReworkCodexExecutor:
    class Process(FakeCodexProcess):
        def run(
            self, request: CodexRunRequest, *, require_clean: bool = True
        ) -> CodexRunResult:
            assert request.task["task_type"] == "REVIEW_REWORK"
            if mode == "correct":
                assert not require_clean
                assert request.previous_run_summary is not None
                assert "PROTECTED_PATH_CHANGE" in request.previous_run_summary
                assert (
                    request.worktree_path / "generated.py"
                ).read_text() == "answer = 42\n"
                assert (
                    request.worktree_path / "partial.py"
                ).read_text() == "keep = True\n"
                (request.worktree_path / ".github/workflows/unauthorised.yml").unlink()
            result = super().run(request, require_clean=require_clean)
            if mode == "empty":
                (request.worktree_path / "generated.py").write_text("answer = 41\n")
            if mode == "reject":
                offending = request.worktree_path / ".github/workflows/unauthorised.yml"
                offending.parent.mkdir(parents=True, exist_ok=True)
                offending.write_text("name: outside scope\n")
                (request.worktree_path / "partial.py").write_text("keep = True\n")
            return result

    return ReviewReworkCodexExecutor(
        application.database,
        lambda connection: Process(
            connection, application.trusted_git, application.data, application.requests
        ),
        timeout_seconds=60,
        cycle_limit=cycle_limit,
    )


def validation_job(application: ReviewApplication) -> Job:
    return replace(
        application.queued,
        id=JobId.generate(),
        job_type="CHANGE_VALIDATE",
        worker_class=WorkerClass.GIT,
        payload={},
        correlation_id=str(uuid4()),
    )


def test_exit_zero_without_changes_blocks_and_never_replaces_jobs(
    tmp_path: Path,
) -> None:
    application = prepared_review(tmp_path)
    outcome = executor(application, mode="empty").execute(application.queued)
    assert outcome.disposition is JobExecutionDisposition.FAILED
    assert outcome.error_id == "codex-empty-review-rework"
    with open_database(application.database) as db:
        assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
        row = db.execute(
            "SELECT process_status,exit_code FROM codex_runs WHERE job_id=?",
            (str(application.queued.id),),
        ).fetchone()
        assert tuple(row) == ("SUCCEEDED", 0)
        count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        for _ in range(3000):
            assert LifecycleCoordinator(db).enqueue_due() == 0
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == count
    assert (application.workspace / "generated.py").read_text() == "answer = 41\n"


def test_actual_changes_and_replay_advance_without_duplicate_execution(
    tmp_path: Path,
) -> None:
    application = prepared_review(tmp_path)
    assert (
        executor(application).execute(application.queued).disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    assert (
        executor(application).execute(application.queued).disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    with open_database(application.database) as db:
        assert (
            db.execute("SELECT state FROM milestones").fetchone()[0]
            == "VALIDATING_CHANGES"
        )
        assert db.execute("SELECT count(*) FROM codex_runs").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM commits").fetchone()[0] == 1
    assert len(application.requests) == 2
    assert (application.workspace / "generated.py").read_text() == "answer = 42\n"


def test_restart_after_process_completion_preserves_output_and_repairs_transition(
    tmp_path: Path,
) -> None:
    application = prepared_review(tmp_path)
    with open_database(application.database) as db:
        db.execute(
            "CREATE TRIGGER interrupt_review_completion BEFORE UPDATE OF state "
            "ON milestones WHEN NEW.state='VALIDATING_CHANGES' "
            "BEGIN SELECT RAISE(ABORT,'simulated crash'); END"
        )
    with pytest.raises(Exception):
        executor(application).execute(application.queued)
    assert len(application.requests) == 2
    assert (application.workspace / "generated.py").read_text() == "answer = 42\n"
    with open_database(application.database) as db:
        db.execute("DROP TRIGGER interrupt_review_completion")
    assert (
        executor(application).execute(application.queued).disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    assert len(application.requests) == 2


@pytest.mark.parametrize("tamper", ["branch", "pr-head"])
def test_replay_rechecks_workspace_and_exact_review_head(
    tmp_path: Path, tamper: str
) -> None:
    application = prepared_review(tmp_path)
    executor(application).execute(application.queued)
    if tamper == "branch":
        git(application.workspace, "checkout", "-b", "unexpected")
    else:
        with open_database(application.database) as db:
            db.execute("UPDATE pull_requests SET head_sha=?", ("f" * 40,))
    result = executor(application).execute(application.queued)
    assert result.disposition is JobExecutionDisposition.FAILED
    assert len(application.requests) == 2


@pytest.mark.parametrize("correction_mode", ["correct", "valid"])
def test_validation_correction_retains_authoritative_review_task_and_findings(
    tmp_path: Path,
    correction_mode: str,
) -> None:
    application = prepared_review(tmp_path)
    with open_database(application.database) as db:
        scheduler = Scheduler(
            SQLiteJobRepository(db, lambda: str(uuid4())),
            WorkerCapacity({worker: 1 for worker in WorkerClass}),
            {WorkerClass.CODEX: executor(application, mode="reject")},
        )
        try:
            scheduler.run_once()
            scheduler.wait_for_wake(5)
            scheduler.run_once()
            assert (
                db.execute(
                    "SELECT state FROM jobs WHERE id=?", (str(application.queued.id),)
                ).fetchone()[0]
                == "SUCCEEDED"
            )
        finally:
            scheduler.close()
    original = application.requests[-1]
    validation = validation_job(application)
    validator = ChangeValidationExecutor(
        application.database, application.trusted_git, application.data
    )
    assert validator.execute(validation).disposition is JobExecutionDisposition.FAILED
    # Replay after the atomic handoff must not save evidence or queue a second job.
    assert validator.execute(validation).disposition is JobExecutionDisposition.FAILED
    with open_database(application.database) as db:
        row = db.execute(
            "SELECT id FROM jobs WHERE job_type='CODEX_REVIEW_REWORK' "
            "AND json_extract(payload_json,'$.validation_id') IS NOT NULL"
        ).fetchone()
        corrected_job = SQLiteJobRepository(db, lambda: str(uuid4())).get(
            JobId.from_string(row[0]), PROJECT
        )
        assert corrected_job.max_attempts == 1
        assert (corrected_job.payload or {})["rework_task_id"] == (
            application.queued.payload or {}
        )["rework_task_id"]
        assert (
            db.execute(
                "SELECT count(*) FROM jobs WHERE job_type='CODEX_RUN'"
            ).fetchone()[0]
            == 1
        )
        before = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        for _ in range(3000):
            assert LifecycleCoordinator(db).enqueue_due() == 0
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == before
        tasks = [
            tuple(row) for row in db.execute("SELECT * FROM architect_rework_tasks")
        ]
        findings = [
            tuple(row) for row in db.execute("SELECT * FROM architect_review_findings")
        ]
        change_count = db.execute("SELECT count(*) FROM change_sets").fetchone()[0]
    result = executor(application, mode=correction_mode).execute(corrected_job)
    if correction_mode == "valid":
        assert result.error_id == "codex-empty-review-rework"
        assert application.requests[-1].task == original.task
        with open_database(application.database) as db:
            count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
            assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
            for _ in range(3000):
                assert LifecycleCoordinator(db).enqueue_due() == 0
            assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == count
        return
    assert result.disposition is JobExecutionDisposition.SUCCEEDED
    assert (
        executor(application, mode="correct").execute(corrected_job).disposition
        is JobExecutionDisposition.SUCCEEDED
    )
    correction = application.requests[-1]
    assert correction.task == original.task
    assert correction.task["findings"] == original.task["findings"]
    assert original.task["findings"]
    assert len(application.requests) == 3
    with open_database(application.database) as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM architect_rework_tasks")
        ] == tasks
        assert [
            tuple(row) for row in db.execute("SELECT * FROM architect_review_findings")
        ] == findings
        assert (
            db.execute("SELECT count(*) FROM change_sets").fetchone()[0] == change_count
        )
    assert (application.workspace / "partial.py").read_text() == "keep = True\n"
    assert (
        validator.execute(
            replace(validation, id=JobId.generate(), correlation_id=str(uuid4()))
        ).disposition
        is JobExecutionDisposition.SUCCEEDED
    )


@pytest.mark.parametrize("phase", ["execution", "validation"])
def test_cycle_exhaustion_blocks_and_cannot_reset_budget(
    tmp_path: Path, phase: str
) -> None:
    application = prepared_review(tmp_path)
    if phase == "execution":
        result = executor(application, cycle_limit=1).execute(application.queued)
        assert result.error_id == "codex-cycle-limit"
        assert len(application.requests) == 1
    else:
        executor(application, mode="reject", cycle_limit=2).execute(application.queued)
        result = ChangeValidationExecutor(
            application.database,
            application.trusted_git,
            application.data,
            cycle_limit=2,
        ).execute(validation_job(application))
        assert len(application.requests) == 2
    assert result.disposition is JobExecutionDisposition.FAILED
    with open_database(application.database) as restarted:
        count = restarted.execute("SELECT count(*) FROM jobs").fetchone()[0]
        for _ in range(3000):
            assert LifecycleCoordinator(restarted).enqueue_due() == 0
        assert restarted.execute("SELECT count(*) FROM jobs").fetchone()[0] == count
        assert (
            restarted.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
        )
