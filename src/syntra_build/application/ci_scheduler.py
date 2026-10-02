# ruff: noqa: E501
"""Lightweight durable-job integration for due CI reconciliations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.application.ci_monitor import CIMonitor, CIMonitorError
from syntra_build.application.scheduler.core import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.projects import ProjectState
from syntra_build.domain.pull_requests import PullRequestState
from syntra_build.infrastructure.persistence.ci import SQLiteCIRepository
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)

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

    def bootstrap(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        correlation_id: str,
        now: datetime,
    ) -> bool:
        """Queue the first reconciliation from exact, trusted durable PR state."""
        project = SQLiteProjectRepository(self.connection, self.id_factory).get(
            project_id
        )
        milestone = SQLiteMilestoneRepository(self.connection, self.id_factory).get(
            milestone_id, project_id
        )
        pull_request = SQLitePullRequestRepository(self.connection).for_milestone(
            milestone_id
        )
        active_pull_request_id = self.connection.execute(
            "SELECT active_pull_request_id FROM milestones WHERE id=?",
            (str(milestone_id),),
        ).fetchone()["active_pull_request_id"]
        if project.state is not ProjectState.BUILDING:
            raise ValueError("CI bootstrap project must be BUILDING")
        if milestone.state is not MilestoneState.CI_RUNNING:
            raise ValueError("CI bootstrap milestone must be CI_RUNNING")
        if (
            pull_request is None
            or pull_request.project_id != project_id
            or pull_request.milestone_id != milestone_id
            or pull_request.state is not PullRequestState.OPEN
            or active_pull_request_id != pull_request.id
            or not pull_request.head_sha.strip()
        ):
            raise ValueError("CI bootstrap requires the exact active OPEN pull request")
        evidence = self.connection.execute(
            """SELECT 1 FROM ci_runs r JOIN pull_requests p ON p.id=r.pull_request_id
            WHERE r.project_id=? AND r.milestone_id=? AND r.head_sha=p.head_sha
            LIMIT 1""",
            (str(project_id), str(milestone_id)),
        ).fetchone()
        active = self.connection.execute(
            """SELECT 1 FROM jobs WHERE project_id=? AND milestone_id=?
            AND job_type=? AND state IN
                ('QUEUED','DISPATCHED','RUNNING','WAITING_EXTERNAL','RETRY_WAIT')
            LIMIT 1""",
            (str(project_id), str(milestone_id), CI_RECONCILE_JOB),
        ).fetchone()
        if evidence is not None or active is not None:
            return False
        self._add_job(project_id, milestone_id, correlation_id, now)
        return True

    def enqueue_due(self, now: datetime) -> int:
        rows = self.connection.execute(
            """SELECT r.project_id,r.milestone_id
            FROM ci_runs r JOIN milestones m ON m.id=r.milestone_id
            JOIN pull_requests p ON p.id=r.pull_request_id
            WHERE m.state='CI_RUNNING' AND r.next_check_at IS NOT NULL
              AND m.active_pull_request_id=p.id AND p.state='OPEN'
              AND r.head_sha=p.head_sha
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
            self._add_job(
                ProjectId.from_string(row["project_id"]),
                MilestoneId.from_string(row["milestone_id"]),
                self.id_factory(),
                now,
            )
        return len(rows)

    def _add_job(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        correlation_id: str,
        now: datetime,
    ) -> None:
        self.jobs.add(
            Job(
                JobId.from_string(self.id_factory()),
                project_id,
                CI_RECONCILE_JOB,
                JobState.QUEUED,
                0,
                now,
                now,
                milestone_id,
                correlation_id=correlation_id,
                max_attempts=1,
                scheduled_at=now,
                worker_class=WorkerClass.CI,
            )
        )


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
        if job.job_type != CI_RECONCILE_JOB:
            raise ValueError("CI reconciliation requires a CI_RECONCILE job")
        if job.worker_class is not WorkerClass.CI:
            raise ValueError("CI reconciliation requires a CI worker")
        if job.milestone_id is None:
            raise ValueError("CI reconciliation requires a milestone-scoped job")
        if job.payload:
            raise ValueError("CI reconciliation does not accept workflow payload")
        # ``execute`` runs in Scheduler's ThreadPoolExecutor. Opening here keeps
        # normal SQLite thread affinity intact: the worker creates, owns and closes
        # this connection, while scheduler repositories retain their control-thread
        # connection.
        with closing(self.connection_factory(self.database_path)) as connection:
            projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
            milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
            pull_requests = SQLitePullRequestRepository(connection)
            try:
                try:
                    project = projects.get(job.project_id)
                    milestone = milestones.get(job.milestone_id, job.project_id)
                except PersistenceError as error:
                    raise CIMonitorError(
                        "CI project or milestone identity is unavailable"
                    ) from error
                pull_request = pull_requests.for_milestone(job.milestone_id)
                active_id = connection.execute(
                    "SELECT active_pull_request_id FROM milestones WHERE id=?",
                    (str(job.milestone_id),),
                ).fetchone()["active_pull_request_id"]
                if project.state is not ProjectState.BUILDING:
                    raise CIMonitorError("CI project must be BUILDING")
                if milestone.state is not MilestoneState.CI_RUNNING:
                    raise CIMonitorError("CI milestone must be CI_RUNNING")
                if (
                    pull_request is None
                    or pull_request.project_id != job.project_id
                    or pull_request.milestone_id != job.milestone_id
                    or pull_request.state is not PullRequestState.OPEN
                    or active_id != pull_request.id
                    or not pull_request.head_sha.strip()
                ):
                    raise CIMonitorError(
                        "CI requires the exact active OPEN pull request"
                    )
                expected_head_sha = pull_request.head_sha
                monitor = self.monitor_factory(connection)
                record = monitor.reconcile(
                    job.project_id,
                    job.milestone_id,
                    job.correlation_id,
                    expected_head_sha=expected_head_sha,
                )
                persisted = SQLiteCIRepository(connection).get(record.id)
                current_pull_request = pull_requests.for_milestone(job.milestone_id)
                if (
                    persisted != record
                    or persisted.project_id != str(job.project_id)
                    or persisted.milestone_id != str(job.milestone_id)
                    or persisted.pull_request_id != pull_request.id
                    or persisted.head_sha != expected_head_sha
                    or persisted.attempt_number < 1
                    or current_pull_request is None
                    or current_pull_request.id != pull_request.id
                    or current_pull_request.head_sha != expected_head_sha
                ):
                    raise CIMonitorError(
                        "persisted CI evidence does not match the expected PR head"
                    )
            except CIMonitorError:
                return JobExecutionResult(
                    JobExecutionDisposition.FAILED,
                    error_id="ci-reconciliation-invariant",
                    failure_classification=FailureClassification.PERMANENT,
                )
        return JobExecutionResult(
            JobExecutionDisposition.SUCCEEDED,
            result={
                "ci_run_id": record.id,
                "head_sha": record.head_sha,
                "overall_status": record.overall_status.value,
                "attempt_number": record.attempt_number,
                "retry_count": record.retry_count,
                "next_check_at": record.next_check_at.isoformat()
                if record.next_check_at
                else None,
            },
        )
