# ruff: noqa: E501
"""Explicit offline operator repair for a paused legacy empty implementation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.application.change_validation import ChangeValidationService
from syntra_build.application.implementation_policy import codex_cycles
from syntra_build.domain import (
    Job,
    JobId,
    JobState,
    MilestoneId,
    MilestoneState,
    MilestoneTransitionRequest,
    ProjectId,
    ProjectState,
    WorkerClass,
)
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.codex import SQLiteCodexRunRepository
from syntra_build.infrastructure.persistence.connection import transaction_scope
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository


def recover_empty_implementation(
    connection: sqlite3.Connection,
    git: TrustedGit,
    data_root: Path,
    project_id: ProjectId,
    milestone_id: MilestoneId,
    *,
    cycle_limit: int,
    now: datetime | None = None,
) -> JobId:
    """Queue exactly one bounded attempt; never resume or invoke a provider."""
    now = now or datetime.now(UTC)
    with transaction_scope(connection):
        project = SQLiteProjectRepository(connection, lambda: str(uuid4())).get(
            project_id
        )
        milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
        milestone = milestones.get(milestone_id, project_id)
        if (
            project.state is not ProjectState.PAUSED
            or project.resume_state is not ProjectState.BUILDING
        ):
            raise ValueError(
                "recovery requires a paused project with BUILDING resume state"
            )
        existing = connection.execute(
            "SELECT id FROM jobs WHERE project_id=? AND milestone_id=? AND job_type='CODEX_RUN' AND json_extract(payload_json,'$.operator_recovery')='legacy-empty-implementation' ORDER BY rowid DESC LIMIT 1",
            (str(project_id), str(milestone_id)),
        ).fetchone()
        if existing is not None:
            return JobId.from_string(existing["id"])
        if milestone.state is not MilestoneState.VALIDATING_CHANGES:
            raise ValueError("recovery requires a legacy VALIDATING_CHANGES milestone")
        if connection.execute(
            "SELECT 1 FROM jobs WHERE project_id=? AND state IN ('QUEUED','DISPATCHED','RUNNING','WAITING_EXTERNAL','RETRY_WAIT')",
            (str(project_id),),
        ).fetchone():
            raise ValueError("reconcile active jobs before operator recovery")
        if connection.execute(
            "SELECT 1 FROM security_events e WHERE project_id=? AND blocking=1 AND NOT EXISTS (SELECT 1 FROM security_event_resolutions r WHERE r.security_event_id=e.id)",
            (str(project_id),),
        ).fetchone():
            raise ValueError("resolve security blockers before operator recovery")
        package = connection.execute(
            "SELECT 1 FROM design_packages p JOIN project_documents s ON s.id=p.spec_document_id JOIN project_documents a ON a.id=p.agents_document_id WHERE p.project_id=? AND p.status='APPROVED' AND s.project_id=p.project_id AND a.project_id=p.project_id AND s.status='APPROVED' AND a.status='APPROVED'",
            (str(project_id),),
        ).fetchone()
        task = connection.execute(
            "SELECT 1 FROM architect_requests q JOIN architect_responses r ON r.architect_request_id=q.id WHERE q.project_id=? AND q.milestone_id=? AND q.request_type='TASK' AND r.status='ACCEPTED' AND json_extract(r.normalised_payload_json,'$.task_type')='IMPLEMENT'",
            (str(project_id), str(milestone_id)),
        ).fetchone()
        if package is None or task is None:
            raise ValueError(
                "approved documents and accepted IMPLEMENT task are required"
            )
        live = ChangeValidationService(connection, git, data_root).replay_evidence(
            project_id, milestone_id
        )
        if live is None or live.collected is None or live.collected.files:
            raise ValueError(
                "recovery requires an identity-verified empty worktree; preserve non-empty work"
            )
        if not SQLiteCodexRunRepository(connection).has_successful_coding_run(
            project_id, milestone_id, live.workspace.id
        ):
            raise ValueError("legacy successful Codex process evidence is required")
        validation = connection.execute(
            "SELECT id FROM change_sets WHERE project_id=? AND milestone_id=? AND worktree_id=? AND head_sha_before_commit=? AND diff_hash=? AND is_empty=1 AND decision='REWORK_REQUIRED' ORDER BY rowid DESC LIMIT 1",
            (
                str(project_id),
                str(milestone_id),
                live.workspace.id,
                live.trusted_head,
                live.collected.canonical_hash,
            ),
        ).fetchone()
        if validation is None:
            raise ValueError(
                "matching historical empty validation evidence is required"
            )
        queued = Job(
            JobId.generate(),
            project_id,
            "CODEX_RUN",
            JobState.QUEUED,
            0,
            now,
            now,
            milestone_id,
            correlation_id=str(uuid4()),
            max_attempts=1,
            scheduled_at=now,
            worker_class=WorkerClass.CODEX,
            payload={
                "validation_id": validation["id"],
                "operator_recovery": "legacy-empty-implementation",
            },
        )
        if codex_cycles(connection, queued) >= cycle_limit:
            raise ValueError(
                "Codex cycle budget exhausted; operator recovery cannot reset it"
            )
        milestones.apply_transition(
            MilestoneTransitionRequest(
                milestone_id,
                project_id,
                MilestoneState.VALIDATING_CHANGES,
                MilestoneState.CODING,
                "Operator recovered legacy empty implementation; project remains paused",
                "HUMAN",
                "syntra-build-admin",
                queued.correlation_id,
                now,
                metadata={"job_id": str(queued.id), "validation_id": validation["id"]},
            )
        )
        SQLiteJobRepository(connection, lambda: str(uuid4())).add(queued)
        return queued.id
