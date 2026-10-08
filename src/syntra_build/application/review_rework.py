"""Durable handoff from Architect findings to the existing Codex worker path."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from syntra_build.application.codex import WorkspaceBoundCodexRunner
from syntra_build.application.implementation_policy import (
    block_implementation,
    codex_cycles,
)
from syntra_build.application.scheduler import (
    JobExecutionDisposition,
    JobExecutionResult,
)
from syntra_build.domain.codex import CodexProcessStatus, CodexRunRequest
from syntra_build.domain.failures import FailureClassification
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.projects import ProjectState
from syntra_build.domain.pull_requests import PullRequestState
from syntra_build.domain.reviews import ArchitectReviewVerdict, ArchitectReworkTask
from syntra_build.domain.workspaces import WorkspaceError, WorkspaceState
from syntra_build.infrastructure.persistence.codex import SQLiteCodexRunRepository
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)
from syntra_build.infrastructure.persistence.reviews import (
    SQLiteArchitectReviewRepository,
)
from syntra_build.infrastructure.persistence.workspaces import SQLiteWorkspaceRepository

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
        runner_factory: Callable[[sqlite3.Connection], WorkspaceBoundCodexRunner],
        *,
        timeout_seconds: float,
        cycle_limit: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        connection_factory: Callable[[Path], sqlite3.Connection] = open_database,
    ) -> None:
        self.database_path, self.runner_factory = database_path, runner_factory
        self.timeout_seconds, self.clock = timeout_seconds, clock
        self.cycle_limit = cycle_limit
        self.connection_factory = connection_factory

    def execute(self, job: Job) -> JobExecutionResult:
        if job.job_type != REVIEW_REWORK_JOB_TYPE:
            raise ValueError(
                "review rework execution requires a CODEX_REVIEW_REWORK job"
            )
        if job.worker_class is not WorkerClass.CODEX:
            raise ValueError("review rework execution requires a Codex worker")
        if job.milestone_id is None:
            raise ValueError("review rework execution requires a milestone-scoped job")
        payload = job.payload
        fields = {"task_type", "review_id", "rework_task_id", "pull_request_id"}
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise ValueError("review rework job payload has an invalid envelope")
        if payload["task_type"] != "REVIEW_REWORK" or any(
            not isinstance(payload[name], str) or not str(payload[name]).strip()
            for name in fields - {"task_type"}
        ):
            raise ValueError("review rework job payload has invalid identity fields")
        with closing(self.connection_factory(self.database_path)) as connection:
            try:
                return self._execute(connection, self.runner_factory(connection), job)
            except ValueError, WorkspaceError:
                return JobExecutionResult(
                    JobExecutionDisposition.FAILED,
                    failure_classification=FailureClassification.PERMANENT,
                )

    def _execute(
        self,
        connection: sqlite3.Connection,
        runner: WorkspaceBoundCodexRunner,
        job: Job,
    ) -> JobExecutionResult:
        assert job.milestone_id is not None
        payload = job.payload
        assert payload is not None
        task_id = cast(str, payload["rework_task_id"])
        review_id = cast(str, payload["review_id"])
        pull_request_id = cast(str, payload["pull_request_id"])
        if (
            connection.execute(
                "SELECT 1 FROM projects WHERE id=?", (str(job.project_id),)
            ).fetchone()
            is None
        ):
            raise ReviewReworkTrustError("review rework project is unavailable")
        if (
            connection.execute(
                "SELECT 1 FROM milestones WHERE id=? AND project_id=?",
                (str(job.milestone_id), str(job.project_id)),
            ).fetchone()
            is None
        ):
            raise ReviewReworkTrustError("review rework milestone is unavailable")
        milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
        project = SQLiteProjectRepository(connection, lambda: str(uuid4())).get(
            job.project_id
        )
        milestone = milestones.get(job.milestone_id, job.project_id)
        if project.state is not ProjectState.BUILDING:
            raise ReviewReworkTrustError("review rework project is not BUILDING")
        if milestone.state not in {
            MilestoneState.CODING,
            MilestoneState.VALIDATING_CHANGES,
        }:
            raise ReviewReworkTrustError(
                "review rework milestone has an inconsistent state"
            )
        row = connection.execute(
            "SELECT * FROM architect_rework_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise ReviewReworkTrustError("durable review rework task is unavailable")
        if (
            row["project_id"] != str(job.project_id)
            or row["milestone_id"] != str(job.milestone_id)
            or row["task_type"] != "REVIEW_REWORK"
            or row["review_id"] != review_id
            or row["pull_request_id"] != pull_request_id
        ):
            raise ReviewReworkTrustError(
                "durable review rework identity is inconsistent"
            )
        try:
            task_payload = json.loads(row["task_payload_json"])
        except json.JSONDecodeError as error:
            raise ReviewReworkTrustError(
                "durable review rework task is malformed"
            ) from error
        task = ArchitectReworkTask.from_dict(task_payload)
        if task.project_id != job.project_id or task.milestone_id != job.milestone_id:
            raise ReviewReworkTrustError("rework task domain identity is inconsistent")

        review = SQLiteArchitectReviewRepository(connection).get(review_id)
        if review is None:
            raise ReviewReworkTrustError("originating Architect review is unavailable")
        review_row = connection.execute(
            """SELECT r.project_id,r.milestone_id,q.request_payload_json
            FROM architect_reviews r JOIN architect_requests q
              ON q.id=r.architect_request_id WHERE r.id=?""",
            (review_id,),
        ).fetchone()
        try:
            review_request = (
                json.loads(review_row["request_payload_json"])
                if review_row is not None
                else None
            )
        except json.JSONDecodeError as error:
            raise ReviewReworkTrustError(
                "originating Architect request is malformed"
            ) from error
        persisted_findings = {
            (
                finding["finding_code"],
                finding["severity"],
                finding["requirement_ref"],
                finding["description"],
                finding["recommended_action"],
            )
            for finding in connection.execute(
                """SELECT finding_code,severity,requirement_ref,description,
                recommended_action FROM architect_review_findings
                WHERE review_id=?""",
                (review_id,),
            ).fetchall()
        }
        task_findings = {
            (
                finding.finding_id,
                finding.severity.value,
                finding.requirement_ref,
                finding.description,
                finding.recommended_action,
            )
            for finding in task.findings
        }
        if (
            review_row is None
            or review_row["project_id"] != str(job.project_id)
            or review_row["milestone_id"] != str(job.milestone_id)
            or review.pull_request_id != pull_request_id
            or review.verdict is not ArchitectReviewVerdict.CHANGES_REQUIRED
            or review.reviewed_sha != task.reviewed_sha
            or review.superseded_at is not None
            or review.finding_count < len(task.findings)
            or not task_findings.issubset(persisted_findings)
            or not isinstance(review_request, dict)
            or review_request.get("agents_instructions") != task.agents_instructions
        ):
            raise ReviewReworkTrustError(
                "originating Architect review is not authoritative"
            )

        pull_request = SQLitePullRequestRepository(connection).for_milestone(
            job.milestone_id
        )
        active = connection.execute(
            "SELECT active_pull_request_id FROM milestones WHERE id=? AND project_id=?",
            (str(job.milestone_id), str(job.project_id)),
        ).fetchone()
        if (
            pull_request is None
            or pull_request.id != pull_request_id
            or pull_request.project_id != job.project_id
            or pull_request.milestone_id != job.milestone_id
            or active is None
            or active["active_pull_request_id"] != pull_request.id
            or pull_request.state is not PullRequestState.OPEN
            or pull_request.external_pr_number != task.pull_request_number
            or pull_request.head_branch != task.branch
            or pull_request.head_sha != task.reviewed_sha
        ):
            raise ReviewReworkTrustError(
                "active pull request does not match reviewed revision"
            )

        workspace = SQLiteWorkspaceRepository(connection).workspace_for_milestone(
            job.milestone_id
        )
        if (
            workspace is None
            or workspace.project_id != job.project_id
            or workspace.milestone_id != job.milestone_id
            or workspace.branch_name != task.branch
            or workspace.current_head_sha != task.reviewed_sha
        ):
            raise ReviewReworkTrustError("workspace does not match reviewed revision")
        # This existing rework job is the sole local Codex owner. M32.15 must keep
        # global composition from enqueueing a generic CODEX_RUN for this milestone.
        request = CodexRunRequest(
            "1.0",
            job.correlation_id,
            job.project_id,
            job.milestone_id,
            job.id,
            job.attempt_number + 1,
            workspace.path,
            task.to_dict(),
            str(task.agents_instructions["content"]),
            self.timeout_seconds,
        )
        runs = SQLiteCodexRunRepository(connection)
        result = runs.completed_for_request(request)
        if result is None and workspace.state is not WorkspaceState.READY:
            raise ReviewReworkTrustError(
                "workspace is not READY for a new Codex process"
            )
        runner.validate_workspace(request, require_clean=result is None)
        if result is None:
            if milestone.state is not MilestoneState.CODING:
                raise ReviewReworkTrustError(
                    "advanced milestone lacks durable Codex success"
                )
            if codex_cycles(connection, job) >= self.cycle_limit:
                block_implementation(
                    connection,
                    job,
                    "Rework attempts exhausted; human intervention required",
                    self.clock(),
                )
                return JobExecutionResult(
                    JobExecutionDisposition.FAILED,
                    error_id="codex-cycle-limit",
                    failure_classification=FailureClassification.PERMANENT,
                )
            returned = runner.run(request)
            result = runs.completed_for_request(request)
            if result is None:
                raise ReviewReworkTrustError("Codex result was not durably persisted")
            self._require_result_identity(request, returned)
            self._require_result_identity(request, result)
            if returned.process_status is not result.process_status:
                raise ReviewReworkTrustError(
                    "Codex result disagrees with durable evidence"
                )
        else:
            self._require_result_identity(request, result)
        if result.process_status is CodexProcessStatus.SUCCEEDED:
            if milestone.state is MilestoneState.CODING:
                now = self.clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ReviewReworkTrustError("review rework clock must return UTC")
                milestones.apply_transition(
                    MilestoneTransitionRequest(
                        job.milestone_id,
                        job.project_id,
                        MilestoneState.CODING,
                        MilestoneState.VALIDATING_CHANGES,
                        "successful Architect review rework Codex run durably recorded",
                        "SYSTEM",
                        "review-rework-codex-executor",
                        job.correlation_id,
                        now.astimezone(UTC),
                        metadata={
                            "job_id": str(job.id),
                            "rework_task_id": task_id,
                            "review_id": review_id,
                            "reviewed_sha": task.reviewed_sha,
                        },
                    )
                )
            return JobExecutionResult(
                JobExecutionDisposition.SUCCEEDED,
                result={"task_type": "REVIEW_REWORK"},
                exit_code=result.exit_code,
                logs_reference=result.stdout_reference,
            )
        if milestone.state is not MilestoneState.CODING:
            raise ReviewReworkTrustError(
                "unsuccessful Codex run cannot accompany advancement"
            )
        disposition = (
            JobExecutionDisposition.CANCELLED
            if result.process_status is CodexProcessStatus.CANCELLED
            else JobExecutionDisposition.FAILED
        )
        classification = {
            CodexProcessStatus.TIMED_OUT: FailureClassification.TRANSIENT,
            CodexProcessStatus.CANCELLED: FailureClassification.CANCELLED,
        }.get(result.process_status, FailureClassification.PERMANENT)
        return JobExecutionResult(
            disposition,
            result={"task_type": "REVIEW_REWORK"},
            exit_code=result.exit_code,
            logs_reference=result.stderr_reference,
            failure_classification=classification,
        )

    @staticmethod
    def _require_result_identity(request: CodexRunRequest, result: object) -> None:
        if not all(
            getattr(result, field) == getattr(request, field)
            for field in (
                "interface_version",
                "correlation_id",
                "project_id",
                "milestone_id",
                "job_id",
                "attempt_number",
            )
        ):
            raise ReviewReworkTrustError("Codex result identity does not match request")


class ReviewReworkTrustError(ValueError):
    """Durable evidence cannot safely authorise review-rework execution."""
