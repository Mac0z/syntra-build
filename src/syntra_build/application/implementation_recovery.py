# ruff: noqa: E501
"""Explicit operator recovery using existing implementation/process evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
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
    ProjectTransitionRequest,
    WorkerClass,
)
from syntra_build.domain.architect_tasks import ArchitectTaskType
from syntra_build.domain.change_validation import ValidationDecision
from syntra_build.infrastructure.git_workspace import TrustedGit
from syntra_build.infrastructure.persistence.architect import (
    SQLiteArchitectInteractionRepository,
)
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


def recover_successful_implementation_handoff(
    connection: sqlite3.Connection,
    git: TrustedGit,
    data_root: Path,
    project_id: ProjectId,
    milestone_id: MilestoneId,
    failed_job_id: JobId,
    *,
    repair_command: Callable[[Path], Sequence[str]],
    now: datetime | None = None,
) -> str:
    """Adopt exact durable process evidence; stage validation without dispatch."""
    now = now or datetime.now(UTC)
    correlation = f"handoff-recovery:{failed_job_id}"
    projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
    milestones = SQLiteMilestoneRepository(connection, lambda: str(uuid4()))
    jobs = SQLiteJobRepository(connection, lambda: str(uuid4()))
    service = ChangeValidationService(connection, git, data_root)

    def guarded_evidence() -> tuple[sqlite3.Row, str | None]:
        project = projects.get(project_id)
        milestone = milestones.get(milestone_id, project_id)
        job = jobs.get(failed_job_id, project_id)
        if (
            job.milestone_id != milestone_id
            or job.job_type != "CODEX_RUN"
            or job.worker_class is not WorkerClass.CODEX
            or job.state is not JobState.FAILED
            or job.last_error_id != "scheduler-worker-execution-failure"
        ):
            raise ValueError("failed job is not an implementation handoff failure")
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
        rows = connection.execute(
            """SELECT r.*,w.worktree_path FROM codex_runs r
               JOIN git_workspaces w ON w.id=r.worktree_id
               JOIN job_attempts a ON a.job_id=r.job_id
                AND a.attempt_number=r.attempt_number
              WHERE r.project_id=? AND r.milestone_id=? AND r.job_id=?
                AND r.attempt_number=? AND r.correlation_id=?
                AND r.process_status='SUCCEEDED' AND r.exit_code=0
                AND r.completed_at IS NOT NULL AND r.worker_identity='syntra-codex'
                AND w.project_id=r.project_id AND w.milestone_id=r.milestone_id
                AND a.state='FAILED' AND a.completed_at IS NOT NULL
                AND a.error_id='scheduler-worker-execution-failure'""",
            (
                str(project_id),
                str(milestone_id),
                str(failed_job_id),
                job.attempt_number,
                job.correlation_id,
            ),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                "exact successful run and failed attempt evidence required"
            )
        run = rows[0]
        expected_path = (
            data_root.resolve() / "workspaces" / str(project_id) / str(milestone_id)
        )
        if Path(run["worktree_path"]) != expected_path:
            raise ValueError("run worktree is not the exact managed milestone path")
        package = connection.execute(
            """SELECT 1 FROM design_packages p
               JOIN project_documents s ON s.id=p.spec_document_id
               JOIN project_documents a ON a.id=p.agents_document_id
               WHERE p.project_id=? AND p.status='APPROVED'
                 AND s.project_id=p.project_id AND a.project_id=p.project_id
                 AND s.status='APPROVED' AND a.status='APPROVED'""",
            (str(project_id),),
        ).fetchone()
        task = SQLiteArchitectInteractionRepository(
            connection
        ).accepted_task_for_milestone(str(project_id), str(milestone_id))
        task_json = json.dumps(task.to_dict(), sort_keys=True, separators=(",", ":"))
        if (
            package is None
            or task.task_type is not ArchitectTaskType.IMPLEMENT
            or task.project_id != project_id
            or task.milestone_id != milestone_id
            or run["task_payload_json"] != task_json
            or run["task_content_hash"]
            != hashlib.sha256(task_json.encode()).hexdigest()
        ):
            raise ValueError("run must match the approved implementation task")
        transition = connection.execute(
            """SELECT * FROM state_transitions WHERE entity_type='MILESTONE'
               AND entity_id=? AND project_id=? ORDER BY rowid DESC LIMIT 1""",
            (str(milestone_id), str(project_id)),
        ).fetchone()
        project_transition = connection.execute(
            """SELECT * FROM state_transitions WHERE entity_type='PROJECT'
               AND entity_id=? ORDER BY rowid DESC LIMIT 1""",
            (str(project_id),),
        ).fetchone()
        completed_id = None
        if (
            transition is not None
            and transition["new_state"] == "COMMITTING"
            and transition["actor_id"] == "syntra-build-admin"
            and transition["correlation_id"] == correlation
        ):
            metadata = json.loads(transition["metadata_json"])
            if (
                metadata.get("operator_recovery") == "successful-implementation-handoff"
                and metadata.get("job_id") == str(failed_job_id)
                and metadata.get("codex_run_id") == run["id"]
                and metadata.get("worktree_id") == run["worktree_id"]
                and project.state is ProjectState.PAUSED
                and project.resume_state is ProjectState.BUILDING
                and milestone.state is MilestoneState.COMMITTING
            ):
                completed_id = metadata.get("change_set_id")
        if completed_id is None and (
            project.state is not ProjectState.BLOCKED
            or project_transition is None
            or project_transition["new_state"] != "BLOCKED"
            or project_transition["previous_state"] != "BUILDING"
            or project_transition["correlation_id"] != job.correlation_id
            or milestone.state is not MilestoneState.BLOCKED
            or milestone.resume_state
            not in {MilestoneState.CODING, MilestoneState.VALIDATING_CHANGES}
            or transition is None
            or transition["new_state"] != "BLOCKED"
            or transition["previous_state"] != milestone.resume_state.value
            or transition["correlation_id"] != job.correlation_id
            or json.loads(transition["metadata_json"]).get("job_id")
            != str(failed_job_id)
        ):
            raise ValueError("persisted blocker is not the applicable failed handoff")
        return run, completed_id

    # Validate identity before the privileged operation; repair never starts Codex.
    with transaction_scope(connection):
        run, completed_id = guarded_evidence()
    try:
        repaired = subprocess.run(
            repair_command(Path(run["worktree_path"])),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except OSError, subprocess.SubprocessError:
        raise ValueError("trusted filesystem handoff repair failed") from None
    if repaired.returncode != 0:
        raise ValueError("trusted filesystem handoff repair failed")
    live = service.replay_evidence(project_id, milestone_id)
    if (
        live is None
        or live.workspace.id != run["worktree_id"]
        or live.collected is None
        or not live.collected.files
    ):
        raise ValueError("recovery requires identity-verified effective source changes")

    diff_hash = live.collected.canonical_hash

    def accepted_record(record_id: str | None = None) -> sqlite3.Row | None:
        row: sqlite3.Row | None = connection.execute(
            """SELECT id FROM change_sets
           WHERE project_id=? AND milestone_id=? AND worktree_id=?
             AND branch_name=? AND base_sha=? AND head_sha_before_commit=?
             AND diff_hash=? AND decision='ACCEPT' AND is_empty=0
             AND correlation_id=? AND scanner_version=? AND policy_version=?
             AND (? IS NULL OR id=?)
           ORDER BY rowid DESC LIMIT 1""",
            (
                str(project_id),
                str(milestone_id),
                run["worktree_id"],
                live.workspace.branch_name,
                live.workspace.base_sha,
                live.trusted_head,
                diff_hash,
                correlation,
                service.scanner.version,
                service.protected_paths.version,
                record_id,
                record_id,
            ),
        ).fetchone()

        return row

    accepted = accepted_record(completed_id)
    if completed_id is not None:
        if accepted is None or accepted["id"] != completed_id:
            raise ValueError(
                "recovered validation no longer matches the effective diff"
            )
        return completed_id
    if accepted is None:
        validation = service.validate(project_id, milestone_id, correlation, now=now)
        if validation.decision is not ValidationDecision.ACCEPT:
            raise ValueError(
                "deterministic validation rejected recovery; evidence retained"
            )
        change_set_id = validation.id
        if validation.diff_hash != live.collected.canonical_hash:
            raise ValueError("worktree changed during recovery validation")
    else:
        change_set_id = accepted["id"]
    with transaction_scope(connection):
        current_run, already_completed = guarded_evidence()
        if current_run["id"] != run["id"]:
            raise ValueError("run identity changed during recovery")
        current = service.replay_evidence(project_id, milestone_id)
        if (
            current is None
            or current.workspace != live.workspace
            or current.collected is None
            or current.collected.canonical_hash != live.collected.canonical_hash
        ):
            raise ValueError(
                "worktree identity or effective diff changed during recovery"
            )
        if already_completed is not None:
            if accepted_record(already_completed) is None:
                raise ValueError("conflicting recovery evidence")
            return already_completed
        metadata = {
            "operator_recovery": "successful-implementation-handoff",
            "job_id": str(failed_job_id),
            "codex_run_id": run["id"],
            "worktree_id": run["worktree_id"],
            "change_set_id": change_set_id,
            "diff_hash": live.collected.canonical_hash,
        }
        # BUILDING is never committed or observable to dispatch before PAUSED.
        projects.apply_transition(
            ProjectTransitionRequest(
                project_id,
                ProjectState.BLOCKED,
                ProjectState.BUILDING,
                "Operator adopted successful Codex handoff",
                "HUMAN",
                "syntra-build-admin",
                correlation,
                now,
            )
        )
        resume = milestones.get(milestone_id, project_id).resume_state
        assert resume is not None
        route = [resume]
        if resume is MilestoneState.CODING:
            route.append(MilestoneState.VALIDATING_CHANGES)
        route.append(MilestoneState.COMMITTING)
        previous = MilestoneState.BLOCKED
        for target in route:
            milestones.apply_transition(
                MilestoneTransitionRequest(
                    milestone_id,
                    project_id,
                    previous,
                    target,
                    "Operator adopted successful process and deterministic validation",
                    "HUMAN",
                    "syntra-build-admin",
                    correlation,
                    now,
                    metadata=metadata,
                )
            )
            previous = target
        projects.apply_transition(
            ProjectTransitionRequest(
                project_id,
                ProjectState.BUILDING,
                ProjectState.PAUSED,
                "Recovered handoff staged; explicit operator resume required",
                "HUMAN",
                "syntra-build-admin",
                correlation,
                now,
            )
        )
    return change_set_id
