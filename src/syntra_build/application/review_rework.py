"""Durable handoff from Architect findings to the existing Codex worker path."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.application.codex import CodexRunner
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.domain.codex import CodexProcessStatus, CodexRunRequest
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository

REVIEW_REWORK_JOB_TYPE = "CODEX_REVIEW_REWORK"


class ReviewReworkCoordinator:
    """Atomically enqueue one Codex-class job and enter CODING."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.connection, self.clock, self.id_factory = connection, clock, id_factory
        self.jobs = SQLiteJobRepository(connection, id_factory)
        self.milestones = SQLiteMilestoneRepository(connection, id_factory)

    def enqueue(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        review_id: str,
        rework_task_id: str,
        pull_request_id: str,
        correlation_id: str,
        now: datetime | None = None,
    ) -> Job:
        occurred_at = now or self.clock()
        existing = self.connection.execute(
            """SELECT id FROM jobs WHERE project_id=? AND milestone_id=?
            AND job_type=? AND json_extract(payload_json,'$.rework_task_id')=?
            ORDER BY created_at,id LIMIT 1""",
            (
                str(project_id),
                str(milestone_id),
                REVIEW_REWORK_JOB_TYPE,
                rework_task_id,
            ),
        ).fetchone()
        if existing is not None:
            return self.jobs.get(JobId.from_string(existing["id"]), project_id)
        milestone = self.milestones.get(milestone_id, project_id)
        if milestone.state is not MilestoneState.REVIEW_REWORK:
            raise ValueError("review rework can only be queued from REVIEW_REWORK")
        job = Job(
            JobId.from_string(self.id_factory()),
            project_id,
            REVIEW_REWORK_JOB_TYPE,
            JobState.QUEUED,
            0,
            occurred_at,
            occurred_at,
            milestone_id,
            priority=1,
            correlation_id=correlation_id,
            max_attempts=1,
            worker_class=WorkerClass.CODEX,
            payload={
                "task_type": "REVIEW_REWORK",
                "review_id": review_id,
                "rework_task_id": rework_task_id,
                "pull_request_id": pull_request_id,
            },
        )
        self.jobs.add(job)
        self.milestones.apply_transition(
            MilestoneTransitionRequest(
                milestone_id,
                project_id,
                MilestoneState.REVIEW_REWORK,
                MilestoneState.CODING,
                "durable Architect review rework queued for Codex",
                "SYSTEM",
                "review-rework-coordinator",
                correlation_id,
                occurred_at,
                metadata={"job_id": str(job.id), "review_id": review_id},
            )
        )
        return job


class ReviewReworkCodexExecutor:
    """Scheduler executor resolving durable task/worktree evidence for Codex."""

    def __init__(
        self,
        database_path: Path,
        runner_factory: Callable[[sqlite3.Connection], CodexRunner],
        *,
        timeout_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path, self.runner_factory = database_path, runner_factory
        self.timeout_seconds, self.clock = timeout_seconds, clock
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if (
            job.job_type != REVIEW_REWORK_JOB_TYPE
            or job.worker_class is not WorkerClass.CODEX
            or job.milestone_id is None
        ):
            raise ValueError("executor received a non-review-rework Codex job")
        with closing(self.connection_factory(self.database_path)) as connection:
            return self._execute(connection, self.runner_factory(connection), job)

    def _execute(
        self, connection: sqlite3.Connection, runner: CodexRunner, job: Job
    ) -> JobExecutionResult:
        assert job.milestone_id is not None
        payload = job.payload or {}
        task_id = payload.get("rework_task_id")
        if not isinstance(task_id, str):
            raise ValueError("review rework job lacks its durable task identity")
        row = connection.execute(
            """SELECT t.task_payload_json,w.worktree_path
            FROM architect_rework_tasks t
            JOIN git_workspaces w ON w.project_id=t.project_id
              AND w.milestone_id=t.milestone_id
            WHERE t.id=? AND t.project_id=? AND t.milestone_id=?
              AND t.task_type='REVIEW_REWORK' AND w.state='READY'""",
            (task_id, str(job.project_id), str(job.milestone_id)),
        ).fetchone()
        if row is None:
            raise ValueError("durable review task or assigned worktree is unavailable")
        task = json.loads(row["task_payload_json"])
        agents = task.get("agents_instructions")
        if not isinstance(agents, dict) or not isinstance(agents.get("content"), str):
            raise ValueError("review rework task lacks approved AGENTS content")
        result = runner.run(
            CodexRunRequest(
                "1.0",
                job.correlation_id,
                job.project_id,
                job.milestone_id,
                job.id,
                job.attempt_number + 1,
                Path(row["worktree_path"]),
                task,
                agents["content"],
                self.timeout_seconds,
            )
        )
        if result.process_status is CodexProcessStatus.SUCCEEDED:
            now = self.clock()
            SQLiteMilestoneRepository(
                connection, lambda: str(uuid4())
            ).apply_transition(
                MilestoneTransitionRequest(
                    job.milestone_id,
                    job.project_id,
                    MilestoneState.CODING,
                    MilestoneState.VALIDATING_CHANGES,
                    "Codex completed Architect review rework",
                    "SYSTEM",
                    "review-rework-codex-executor",
                    job.correlation_id,
                    now,
                    metadata={"job_id": str(job.id)},
                )
            )
            return JobExecutionResult(
                JobExecutionDisposition.SUCCEEDED,
                result={"task_type": "REVIEW_REWORK"},
                exit_code=result.exit_code,
                logs_reference=result.stdout_reference,
            )
        disposition = (
            JobExecutionDisposition.CANCELLED
            if result.process_status is CodexProcessStatus.CANCELLED
            else JobExecutionDisposition.FAILED
        )
        return JobExecutionResult(
            disposition,
            result={"task_type": "REVIEW_REWORK"},
            exit_code=result.exit_code,
            logs_reference=result.stderr_reference,
        )
