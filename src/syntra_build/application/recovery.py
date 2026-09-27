"""M27 startup recovery coordinator.

The coordinator deliberately has no provider mutation API.  State-specific handlers
may observe providers and persist reconciled evidence; an absent or failed handler
fails closed for mutation-bearing states rather than replaying them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from syntra_build.application.scheduler.core import Scheduler
from syntra_build.domain.identifiers import JobId
from syntra_build.domain.job_state_machine import JobTransitionRequest
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.project_state_machine import ProjectTransitionRequest
from syntra_build.domain.projects import ProjectState
from syntra_build.domain.recovery import (
    RecoveryDecision,
    RecoveryDisposition,
    RecoveryLifecycle,
    RecoverySubject,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.recovery import SQLiteRecoveryRepository


class RecoveryHandler(Protocol):
    """One idempotent, observation-first reconciliation seam."""

    def __call__(
        self, subject: RecoverySubject, correlation_id: str
    ) -> RecoveryDecision: ...


_UNSAFE_STATES = frozenset(
    {"COMMITTING", "PUSHING", "PR_CREATING", "MERGING", "MERGE_VERIFY"}
)
_HUMAN_STATES = frozenset({"HUMAN_DECISION", "HUMAN_TEST"})
_EXTERNAL_WAIT_STATES = frozenset({"CI_RUNNING", "ARCHITECT_REVIEW"})


class RecoveryCoordinator:
    """Drain scheduling, reconcile projects independently, then release dispatch."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        scheduler: Scheduler,
        *,
        handlers: Mapping[str, RecoveryHandler] | None = None,
        workspace_clean: Callable[[RecoverySubject], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.connection, self.scheduler = connection, scheduler
        self.handlers = dict(handlers or {})
        self.workspace_clean = workspace_clean
        self.clock, self.id_factory = clock, id_factory
        self.repository = SQLiteRecoveryRepository(connection, id_factory)
        self.jobs = SQLiteJobRepository(connection, id_factory)
        self.milestones = SQLiteMilestoneRepository(connection, id_factory)
        self.projects = SQLiteProjectRepository(connection, id_factory)
        self.lifecycle = RecoveryLifecycle.STARTING

    def recover(self, *, correlation_id: str | None = None) -> str:
        """Run a bounded startup pass; project failures never abort sibling recovery."""
        correlation_id = correlation_id or f"recovery-{self.id_factory()}"
        self.scheduler.enter_drain()
        self.lifecycle = RecoveryLifecycle.RECOVERING
        now = self.clock()
        with transaction(self.connection):
            run_id = self.repository.begin(correlation_id, now)
        try:
            # Prove the barrier through the actual scheduler seam. This harvests
            # completions but drain mode cannot claim queued privileged work.
            self.scheduler.run_once()
            for subject in self.repository.discover():
                decision = self._reconcile(subject, correlation_id)
                with transaction(self.connection):
                    self.repository.observe(
                        run_id, subject, decision, correlation_id, self.clock()
                    )
            with transaction(self.connection):
                self.repository.finish(run_id, self.clock())
        except Exception as error:
            with transaction(self.connection):
                self.repository.finish(run_id, self.clock(), str(error))
            # A global database/coordinator failure keeps the drain barrier closed.
            raise
        self.lifecycle = RecoveryLifecycle.READY
        self.scheduler.exit_drain()
        return run_id

    def _reconcile(
        self, subject: RecoverySubject, correlation_id: str
    ) -> RecoveryDecision:
        state = subject.milestone_state or subject.project_state
        handler = self.handlers.get(state)
        try:
            if handler is not None:
                return handler(subject, correlation_id)
            if subject.job_id is not None and subject.job_state in {
                "DISPATCHED",
                "RUNNING",
            }:
                return self._lost_local_job(subject, correlation_id)
            if state in _HUMAN_STATES:
                return self._restore_human(subject)
            if (
                state in _EXTERNAL_WAIT_STATES
                or subject.job_state == "WAITING_EXTERNAL"
            ):
                return RecoveryDecision(
                    state,
                    RecoveryDisposition.RESTORED_WAIT,
                    "existing external wait retained",
                )
            if state in _UNSAFE_STATES:
                return self._block(
                    subject, correlation_id, "observation handler unavailable"
                )
            return RecoveryDecision(
                state, RecoveryDisposition.NO_ACTION, "no mutation required"
            )
        except Exception as error:
            return self._block(subject, correlation_id, str(error))

    def _restore_human(self, subject: RecoverySubject) -> RecoveryDecision:
        gate = self.connection.execute(
            """SELECT id,state,correlation_id FROM human_gates
               WHERE project_id=? AND (? IS NULL OR milestone_id=?)
               ORDER BY created_at DESC LIMIT 1""",
            (
                str(subject.project_id),
                str(subject.milestone_id) if subject.milestone_id else None,
                str(subject.milestone_id) if subject.milestone_id else None,
            ),
        ).fetchone()
        if gate is None:
            return RecoveryDecision(
                "HUMAN_GATE",
                RecoveryDisposition.UNKNOWN,
                "no gate created",
                reason="waiting state has no durable gate",
            )
        return RecoveryDecision(
            "HUMAN_GATE",
            RecoveryDisposition.RESTORED_WAIT,
            "existing gate restored without notification replay",
            {"gate_id": gate["id"], "gate_state": gate["state"]},
        )

    def _lost_local_job(
        self, subject: RecoverySubject, correlation_id: str
    ) -> RecoveryDecision:
        assert subject.job_id is not None and subject.job_state is not None
        job = self.jobs.get(subject.job_id, subject.project_id)
        if job.worker_class not in {
            WorkerClass.CODEX,
            WorkerClass.GIT,
            WorkerClass.ARCHITECT,
        }:
            return RecoveryDecision(
                job.job_type,
                RecoveryDisposition.UNKNOWN,
                "non-local execution requires explicit observer",
            )
        clean = (
            self.workspace_clean(subject) if self.workspace_clean is not None else False
        )
        with transaction(self.connection):
            abandoned = self.jobs.abandon(
                JobTransitionRequest(
                    job.id,
                    job.project_id,
                    job.state,
                    JobState.ABANDONED,
                    "local process did not survive restart",
                    "RECOVERY",
                    "startup-recovery",
                    correlation_id,
                    self.clock(),
                    metadata={"worktree_preserved": True},
                    error_id="RECOVERY_PROCESS_LOST",
                )
            )
            replacement = self._replacement(job, correlation_id) if clean else None
        return RecoveryDecision(
            job.job_type,
            RecoveryDisposition.ABANDONED,
            "attempt abandoned; worktree preserved"
            + ("; replacement queued" if replacement else ""),
            {
                "attempt_number": abandoned.attempt_number,
                "workspace_clean": clean,
                "replacement_job_id": str(replacement.id) if replacement else None,
            },
            None
            if clean
            else "workspace is dirty, missing, or could not be proven clean",
        )

    def _replacement(self, prior: Job, correlation_id: str) -> Job:
        existing = self.connection.execute(
            """SELECT id FROM jobs WHERE project_id=? AND milestone_id IS ?
               AND job_type=? AND state IN
               ('QUEUED','DISPATCHED','RUNNING','WAITING_EXTERNAL','RETRY_WAIT')
               ORDER BY created_at LIMIT 1""",
            (
                str(prior.project_id),
                str(prior.milestone_id) if prior.milestone_id else None,
                prior.job_type,
            ),
        ).fetchone()
        if existing is not None:
            return self.jobs.get(JobId.from_string(existing["id"]), prior.project_id)
        now = self.clock()
        replacement = Job(
            JobId.from_string(self.id_factory()),
            prior.project_id,
            prior.job_type,
            JobState.QUEUED,
            0,
            now,
            now,
            prior.milestone_id,
            prior.priority,
            correlation_id,
            prior.max_attempts,
            scheduled_at=now,
            timeout_seconds=prior.timeout_seconds,
            worker_class=prior.worker_class,
            payload={**dict(prior.payload or {}), "replaces_job_id": str(prior.id)},
        )
        self.jobs.add(replacement)
        return replacement

    def _block(
        self, subject: RecoverySubject, correlation_id: str, reason: str
    ) -> RecoveryDecision:
        now = self.clock()
        with transaction(self.connection):
            if subject.milestone_id is not None and subject.milestone_state not in {
                "BLOCKED",
                "FAILED",
                "CANCELLED",
                "COMPLETE",
            }:
                assert subject.milestone_state is not None
                self.milestones.apply_transition(
                    MilestoneTransitionRequest(
                        subject.milestone_id,
                        subject.project_id,
                        MilestoneState(subject.milestone_state),
                        MilestoneState.BLOCKED,
                        "startup recovery could not prove a safe continuation",
                        "RECOVERY",
                        "startup-recovery",
                        correlation_id,
                        now,
                        metadata={"recovery_reason": reason[:200]},
                    )
                )
            if subject.project_state not in {"BLOCKED", "PAUSED"}:
                self.projects.apply_transition(
                    ProjectTransitionRequest(
                        subject.project_id,
                        ProjectState(subject.project_state),
                        ProjectState.BLOCKED,
                        "startup recovery blocked an ambiguous workflow",
                        "RECOVERY",
                        "startup-recovery",
                        correlation_id,
                        now,
                    )
                )
        return RecoveryDecision(
            subject.milestone_state or "PROJECT",
            RecoveryDisposition.BLOCKED,
            "affected workflow held for manual recovery",
            reason=reason,
            observed={
                "project_id": str(subject.project_id),
                "correlation_id": correlation_id,
            },
        )
