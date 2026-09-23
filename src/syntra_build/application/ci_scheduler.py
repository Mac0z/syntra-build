# ruff: noqa: E501
"""Lightweight durable-job integration for due CI reconciliations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.scheduler.core import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository

CI_RECONCILE_JOB = "CI_RECONCILE"


class CIJobCoordinator:
    """Enqueue one CI-class job per due active milestone; never a Codex job."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.connection = connection
        self.id_factory = id_factory
        self.jobs = SQLiteJobRepository(connection, id_factory)

    def enqueue_due(self, now: datetime) -> int:
        rows = self.connection.execute(
            """SELECT r.project_id,r.milestone_id
            FROM ci_runs r JOIN milestones m ON m.id=r.milestone_id
            WHERE m.state='CI_RUNNING' AND r.next_check_at IS NOT NULL
              AND r.next_check_at<=?
              AND r.id=(SELECT newest.id FROM ci_runs newest
                        WHERE newest.milestone_id=r.milestone_id
                        ORDER BY newest.started_at DESC,newest.attempt_number DESC LIMIT 1)
              AND NOT EXISTS (SELECT 1 FROM jobs j
                    WHERE j.milestone_id=r.milestone_id AND j.job_type=?
                      AND j.state IN ('QUEUED','DISPATCHED','RUNNING','WAITING_EXTERNAL','RETRY_WAIT'))""",
            (now.isoformat(), CI_RECONCILE_JOB),
        ).fetchall()
        for row in rows:
            identifier = self.id_factory()
            self.jobs.add(
                Job(
                    JobId.from_string(identifier),
                    ProjectId.from_string(row["project_id"]),
                    CI_RECONCILE_JOB,
                    JobState.QUEUED,
                    0,
                    now,
                    now,
                    MilestoneId.from_string(row["milestone_id"]),
                    correlation_id=identifier,
                    max_attempts=1,
                    scheduled_at=now,
                    worker_class=WorkerClass.CI,
                )
            )
        return len(rows)


class CIReconciliationExecutor:
    """Create all SQLite-owning reconciliation objects on the worker thread."""

    def __init__(
        self,
        database_path: Path,
        monitor_factory: Callable[[sqlite3.Connection], CIMonitor],
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path = database_path
        self.monitor_factory = monitor_factory
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.worker_class is not WorkerClass.CI or job.milestone_id is None:
            raise ValueError("CI reconciliation requires a CI milestone job")
        # ``execute`` runs in Scheduler's ThreadPoolExecutor. Opening here keeps
        # normal SQLite thread affinity intact: the worker creates, owns and closes
        # this connection, while scheduler repositories retain their control-thread
        # connection.
        with closing(self.connection_factory(self.database_path)) as connection:
            monitor = self.monitor_factory(connection)
            record = monitor.reconcile(
                job.project_id, job.milestone_id, job.correlation_id
            )
        return JobExecutionResult(
            JobExecutionDisposition.SUCCEEDED,
            result={
                "ci_run_id": record.id,
                "head_sha": record.head_sha,
                "overall_status": record.overall_status.value,
            },
        )
