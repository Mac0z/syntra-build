# ruff: noqa: E501
"""Bounded implementation handoffs based exclusively on durable evidence."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from uuid import uuid4

from syntra_build.domain import (
    Job,
    JobId,
    JobState,
    MilestoneState,
    MilestoneTransitionRequest,
    ProjectState,
    ProjectTransitionRequest,
    WorkerClass,
)
from syntra_build.infrastructure.persistence.connection import transaction_scope
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

_LOGGER = logging.getLogger(__name__)


def codex_cycles(connection: sqlite3.Connection, job: Job) -> int:
    """Count paid invocations across job identities, retries and rework routes."""
    return int(
        connection.execute(
            "SELECT count(*) FROM codex_runs WHERE project_id=? AND milestone_id=?",
            (str(job.project_id), str(job.milestone_id)),
        ).fetchone()[0]
    )


def block_implementation(
    connection: sqlite3.Connection, job: Job, reason: str, now: datetime
) -> None:
    assert job.milestone_id is not None
    milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
    projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
    with transaction_scope(connection):
        milestone = milestones.get(job.milestone_id, job.project_id)
        project = projects.get(job.project_id)
        if milestone.state is not MilestoneState.BLOCKED:
            milestones.apply_transition(
                MilestoneTransitionRequest(
                    job.milestone_id,
                    job.project_id,
                    milestone.state,
                    MilestoneState.BLOCKED,
                    reason,
                    "SYSTEM",
                    "implementation-policy",
                    job.correlation_id,
                    now,
                    metadata={"job_id": str(job.id)},
                )
            )
        if project.state is ProjectState.BUILDING:
            projects.apply_transition(
                ProjectTransitionRequest(
                    job.project_id,
                    project.state,
                    ProjectState.BLOCKED,
                    reason,
                    "SYSTEM",
                    "implementation-policy",
                    job.correlation_id,
                    now,
                )
            )
    _LOGGER.warning(
        "Implementation requires human intervention",
        extra={
            "event": "implementation_blocked",
            "metadata": {
                "project_id": str(job.project_id),
                "milestone_id": str(job.milestone_id),
                "job_id": str(job.id),
                "correlation_id": job.correlation_id,
                "reason": reason,
            },
        },
    )


def validation_feedback(connection: sqlite3.Connection, job: Job) -> str | None:
    validation_id = (job.payload or {}).get("validation_id")
    if validation_id is None:
        return None
    row = connection.execute(
        "SELECT * FROM change_sets WHERE id=? AND project_id=? AND milestone_id=? AND decision='REWORK_REQUIRED'",
        (validation_id, str(job.project_id), str(job.milestone_id)),
    ).fetchone()
    workspace = connection.execute(
        "SELECT id,current_head_sha,base_sha FROM git_workspaces WHERE project_id=? AND milestone_id=?",
        (str(job.project_id), str(job.milestone_id)),
    ).fetchone()
    if (
        row is None
        or workspace is None
        or row["worktree_id"] != workspace["id"]
        or row["head_sha_before_commit"]
        != (workspace["current_head_sha"] or workspace["base_sha"])
    ):
        raise ValueError("validation rework does not match assigned workspace")
    findings = connection.execute(
        "SELECT finding_code,message,remediation FROM validation_findings WHERE change_set_id=? ORDER BY id",
        (validation_id,),
    ).fetchall()
    return json.dumps(
        {
            "validation_id": validation_id,
            "diff_hash": row["diff_hash"],
            "instruction": "Correct these findings in the existing uncommitted worktree. Preserve existing implementation work. Do not commit or push.",
            "findings": [dict(item) for item in findings],
        },
        sort_keys=True,
    )


def queue_validation_rework(
    connection: sqlite3.Connection,
    job: Job,
    validation_id: str,
    cycle_limit: int,
    now: datetime,
) -> None:
    """One durable handoff per validation; no fresh infrastructure retry budget."""
    assert job.milestone_id is not None
    with transaction_scope(connection):
        if connection.execute(
            "SELECT 1 FROM jobs WHERE project_id=? AND milestone_id=? AND job_type='CODEX_RUN' AND json_extract(payload_json,'$.validation_id')=?",
            (str(job.project_id), str(job.milestone_id), validation_id),
        ).fetchone():
            return
        if codex_cycles(connection, job) >= cycle_limit:
            block_implementation(
                connection,
                job,
                "Rework attempts exhausted; human intervention required",
                now,
            )
            return
        milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
        milestones.apply_transition(
            MilestoneTransitionRequest(
                job.milestone_id,
                job.project_id,
                MilestoneState.VALIDATING_CHANGES,
                MilestoneState.CODING,
                "Implementation validation requires rework",
                "SYSTEM",
                "implementation-policy",
                job.correlation_id,
                now,
                metadata={"validation_id": validation_id},
            )
        )
        queued = Job(
            JobId.generate(),
            job.project_id,
            "CODEX_RUN",
            JobState.QUEUED,
            0,
            now,
            now,
            job.milestone_id,
            correlation_id=str(uuid4()),
            max_attempts=1,
            scheduled_at=now,
            worker_class=WorkerClass.CODEX,
            payload={"validation_id": validation_id},
        )
        SQLiteJobRepository(connection, lambda: str(uuid4())).add(queued)
        validation_feedback(
            connection, queued
        )  # Verify the persisted binding before commit.
        _LOGGER.info(
            "Implementation validation requires rework",
            extra={
                "event": "implementation_rework_queued",
                "metadata": {
                    "project_id": str(job.project_id),
                    "milestone_id": str(job.milestone_id),
                    "job_id": str(queued.id),
                    "validation_job_id": str(job.id),
                    "validation_id": validation_id,
                    "correlation_id": job.correlation_id,
                },
            },
        )
