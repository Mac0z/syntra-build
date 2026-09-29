# ruff: noqa: E501
"""Thin, thread-owned Scheduler adapters for released M32 services."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from syntra_build.application.architect import (
    ArchitectDesignService,
    ArchitectError,
    ArchitectFailureKind,
)
from syntra_build.application.codex import CodexRunner
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.application.specification import SpecificationDraftService
from syntra_build.domain import Job, WorkerClass
from syntra_build.domain.codex import CodexProcessStatus, CodexRunRequest
from syntra_build.domain.failures import FailureClassification
from syntra_build.infrastructure.persistence.architect import (
    SQLiteArchitectInteractionRepository,
)
from syntra_build.infrastructure.persistence.connection import open_database


class ArchitectDesignExecutor:
    """Run one persisted project design request on a worker-owned connection."""

    def __init__(
        self,
        database_path: Path,
        service_factory: Callable[[sqlite3.Connection], ArchitectDesignService],
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.service_factory = service_factory
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "ARCHITECT_DESIGN":
            raise ValueError("Architect design requires an ARCHITECT_DESIGN job")
        if job.worker_class is not WorkerClass.ARCHITECT:
            raise ValueError("Architect design requires an Architect worker")
        if job.milestone_id is not None:
            raise ValueError("Architect design requires a project-scoped job")

        # Scheduler calls execute on its worker pool. Constructing the service from
        # this connection keeps every SQLite repository on the executing thread.
        try:
            with closing(self.connection_factory(self.database_path)) as connection:
                self.service_factory(connection).design(
                    job.project_id, job.correlation_id
                )
        except ArchitectError as error:
            transient = {
                ArchitectFailureKind.TIMEOUT,
                ArchitectFailureKind.TRANSIENT_PROVIDER,
                ArchitectFailureKind.THROTTLED,
            }
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id=f"architect-design-{error.kind.value.casefold()}",
                failure_classification=(
                    FailureClassification.TRANSIENT
                    if error.kind in transient
                    else FailureClassification.PERMANENT
                ),
            )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)


class SpecificationDraftExecutor:
    """Generate one project specification on a worker-owned connection."""

    def __init__(
        self,
        database_path: Path,
        service_factory: Callable[[sqlite3.Connection], SpecificationDraftService],
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.service_factory = service_factory
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != "SPECIFICATION_DRAFT":
            raise ValueError(
                "Specification drafting requires a SPECIFICATION_DRAFT job"
            )
        if job.worker_class is not WorkerClass.ARCHITECT:
            raise ValueError("Specification drafting requires an Architect worker")
        if job.milestone_id is not None:
            raise ValueError("Specification drafting requires a project-scoped job")

        try:
            with closing(self.connection_factory(self.database_path)) as connection:
                self.service_factory(connection).generate(
                    job.project_id, job.correlation_id
                )
        except ArchitectError as error:
            transient = {
                ArchitectFailureKind.TIMEOUT,
                ArchitectFailureKind.TRANSIENT_PROVIDER,
                ArchitectFailureKind.THROTTLED,
            }
            return JobExecutionResult(
                JobExecutionDisposition.FAILED,
                error_id=f"specification-draft-{error.kind.value.casefold()}",
                failure_classification=(
                    FailureClassification.TRANSIENT
                    if error.kind in transient
                    else FailureClassification.PERMANENT
                ),
            )
        return JobExecutionResult(JobExecutionDisposition.SUCCEEDED)


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
