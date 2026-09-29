# ruff: noqa: E501
"""Thin, thread-owned Scheduler adapters for released M32 services."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from syntra_build.application.codex import CodexRunner
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.domain import Job, WorkerClass
from syntra_build.domain.codex import CodexProcessStatus, CodexRunRequest
from syntra_build.infrastructure.persistence.architect import (
    SQLiteArchitectInteractionRepository,
)
from syntra_build.infrastructure.persistence.connection import open_database


class InitialCodexExecutor:
    """Load accepted TASK evidence on the worker thread, then invoke M20."""

    def __init__(
        self,
        database_path: Path,
        runner_factory: Callable[[sqlite3.Connection], CodexRunner],
        *,
        timeout_seconds: float,
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.runner_factory = runner_factory
        self.timeout_seconds = timeout_seconds
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.worker_class is not WorkerClass.CODEX or job.milestone_id is None:
            raise ValueError("CODEX_RUN requires a Codex milestone job")
        with closing(self.connection_factory(self.database_path)) as connection:
            task = SQLiteArchitectInteractionRepository(
                connection
            ).accepted_task_for_milestone(str(job.project_id), str(job.milestone_id))
            row = connection.execute(
                """SELECT w.worktree_path,d.content FROM git_workspaces w
                   JOIN design_packages p ON p.project_id=w.project_id AND p.status='APPROVED'
                   JOIN project_documents d ON d.id=p.agents_document_id
                   WHERE w.project_id=? AND w.milestone_id=? AND w.state='READY'""",
                (str(job.project_id), str(job.milestone_id)),
            ).fetchone()
            if row is None:
                raise ValueError("accepted AGENTS or assigned workspace is unavailable")
            result = self.runner_factory(connection).run(
                CodexRunRequest(
                    "1.0",
                    job.correlation_id,
                    job.project_id,
                    job.milestone_id,
                    job.id,
                    job.attempt_number + 1,
                    Path(row["worktree_path"]),
                    task.to_dict(),
                    row["content"],
                    self.timeout_seconds,
                )
            )
        disposition = (
            JobExecutionDisposition.SUCCEEDED
            if result.process_status is CodexProcessStatus.SUCCEEDED
            else JobExecutionDisposition.FAILED
        )
        return JobExecutionResult(
            disposition,
            exit_code=result.exit_code,
            logs_reference=result.stdout_reference
            if disposition is JobExecutionDisposition.SUCCEEDED
            else result.stderr_reference,
        )
