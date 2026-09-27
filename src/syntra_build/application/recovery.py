"""M27 startup recovery coordinator.

The coordinator deliberately has no provider mutation API.  State-specific handlers
may observe providers and persist reconciled evidence; an absent or failed handler
fails closed for mutation-bearing states rather than replaying them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from syntra_build.application.ci_handoff import PullRequestCIHandoff
from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.gatekeeper import Gatekeeper
from syntra_build.application.human_intervention import HumanInterventionService
from syntra_build.application.review_rework import ReviewReworkCoordinator
from syntra_build.application.scheduler.core import Scheduler
from syntra_build.application.workspaces import WorkspaceService
from syntra_build.domain.ci import CIOverallStatus
from syntra_build.domain.identifiers import JobId
from syntra_build.domain.job_state_machine import JobTransitionRequest
from syntra_build.domain.jobs import Job, JobState, WorkerClass
from syntra_build.domain.merges import (
    MERGE_INTERFACE_VERSION,
    MergeRequest,
    MergeStatus,
    MergeStrategy,
)
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


@dataclass(frozen=True, slots=True)
class RecoveryServices:
    """Trusted M22–M26 services used by concrete recovery paths."""

    pull_requests: PullRequestCIHandoff | None = None
    ci: CIMonitor | None = None
    gatekeeper: Gatekeeper | None = None
    workspace: WorkspaceService | None = None
    human: HumanInterventionService | None = None


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
        services: RecoveryServices | None = None,
        workspace_clean: Callable[[RecoverySubject], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.connection, self.scheduler = connection, scheduler
        self.handlers = dict(handlers or {})
        self.services = services or RecoveryServices()
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
            if subject.job_id is not None and subject.job_state in {
                "DISPATCHED",
                "RUNNING",
            }:
                return self._lost_local_job(subject, correlation_id)
            if subject.job_id is not None:
                return RecoveryDecision(
                    subject.job_state or "JOB",
                    RecoveryDisposition.RESTORED_WAIT,
                    "durable non-local wait retained",
                )
            concrete = self._concrete(subject, correlation_id)
            if concrete is not None:
                return concrete
            if handler is not None:
                return handler(subject, correlation_id)
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

    def _concrete(
        self, subject: RecoverySubject, correlation_id: str
    ) -> RecoveryDecision | None:
        state = subject.milestone_state
        if subject.milestone_id is None or state is None:
            return None
        if state == "PR_CREATING" and self.services.pull_requests is not None:
            row = self.connection.execute(
                """SELECT cs.id FROM change_sets cs JOIN commits c
                   ON c.change_set_id=cs.id
                   WHERE cs.project_id=? AND cs.milestone_id=? AND cs.decision='ACCEPT'
                   ORDER BY cs.created_at DESC,cs.id LIMIT 1""",
                (str(subject.project_id), str(subject.milestone_id)),
            ).fetchone()
            if row is None:
                raise ValueError("PR recovery lacks accepted change-set/commit intent")
            record = self.services.pull_requests.establish(
                subject.project_id,
                subject.milestone_id,
                row["id"],
                correlation_id,
                now=self.clock(),
            )
            return RecoveryDecision(
                "PR_CREATING",
                RecoveryDisposition.RECONCILED,
                "trusted PR adopted and CI handoff restored",
                {"pull_request_id": record.id, "head_sha": record.head_sha},
            )
        if state == "CI_RUNNING" and self.services.ci is not None:
            pr = self.connection.execute(
                "SELECT head_sha FROM pull_requests WHERE milestone_id=?",
                (str(subject.milestone_id),),
            ).fetchone()
            if pr is None:
                raise ValueError("CI recovery lacks trusted pull request")
            run = self.services.ci.reconcile(
                subject.project_id,
                subject.milestone_id,
                correlation_id,
                expected_head_sha=pr["head_sha"],
            )
            disposition = (
                RecoveryDisposition.RECONCILED
                if run.overall_status
                in {CIOverallStatus.PASSED, CIOverallStatus.FAILED}
                else RecoveryDisposition.RESTORED_WAIT
            )
            return RecoveryDecision(
                "CI_RUNNING",
                disposition,
                "existing exact-head CI reconciled",
                {
                    "ci_run_id": run.id,
                    "head_sha": run.head_sha,
                    "status": run.overall_status.value,
                },
            )
        if state in {"COMMITTING", "PUSHING"} and self.services.workspace is not None:
            commit = self.connection.execute(
                """SELECT c.commit_sha,c.pushed_at,c.change_set_id
                   FROM commits c JOIN change_sets cs ON cs.id=c.change_set_id
                   WHERE c.project_id=? AND c.milestone_id=? AND cs.decision='ACCEPT'
                   ORDER BY c.created_at DESC,c.id LIMIT 1""",
                (str(subject.project_id), str(subject.milestone_id)),
            ).fetchone()
            if commit is None:
                raise ValueError("Git recovery cannot identify one accepted commit")
            inspection = self.services.workspace.inspect(
                subject.project_id, subject.milestone_id, self.clock()
            )
            if inspection.head_sha != commit["commit_sha"] or not inspection.clean:
                raise ValueError("workspace does not match the exact accepted commit")
            if state == "COMMITTING":
                self._advance_milestone(
                    subject,
                    MilestoneState.PUSHING,
                    correlation_id,
                    "recovery adopted exact existing trusted commit",
                )
                return RecoveryDecision(
                    state,
                    RecoveryDisposition.RECONCILED,
                    "existing trusted commit adopted without committing",
                    {"commit_sha": commit["commit_sha"]},
                )
            pushed = self.services.workspace.push(
                subject.project_id,
                subject.milestone_id,
                commit["commit_sha"],
                self.clock(),
            )
            self._advance_milestone(
                subject,
                MilestoneState.PR_CREATING,
                correlation_id,
                "recovery verified exact remote branch commit",
            )
            return RecoveryDecision(
                state,
                RecoveryDisposition.RECONCILED,
                "remote branch reconciled without force push",
                {"commit_sha": pushed.commit_sha, "remote_sha": pushed.remote_sha},
            )
        if state in {"MERGING", "MERGE_VERIFY"} and self.services.gatekeeper:
            return self._recover_merge(subject, correlation_id)
        if state in _HUMAN_STATES and self.services.human is not None:
            gate = self.services.human.recover_gate(
                subject.project_id, subject.milestone_id, occurred_at=self.clock()
            )
            return RecoveryDecision(
                "HUMAN_GATE",
                RecoveryDisposition.RESTORED_WAIT,
                "durable human gate restored",
                {"gate_id": str(gate.id), "gate_state": gate.state.value},
            )
        if state in {"ARCHITECT_REVIEW", "REVIEW_REWORK"}:
            review = self.connection.execute(
                """SELECT r.* FROM architect_reviews r
                   WHERE r.project_id=? AND r.milestone_id=?
                   ORDER BY r.created_at DESC,r.id LIMIT 1""",
                (str(subject.project_id), str(subject.milestone_id)),
            ).fetchone()
            if review is None:
                return None
            if (
                review["verdict"] in {"HUMAN_TEST_REQUIRED", "HUMAN_DECISION_REQUIRED"}
                and self.services.human is not None
            ):
                gate = self.services.human.reconcile_review(
                    review["id"], occurred_at=self.clock()
                )
                return RecoveryDecision(
                    "ARCHITECT_HANDOFF",
                    RecoveryDisposition.RECONCILED,
                    "persisted review handoff reconciled without Architect call",
                    {"review_id": review["id"], "gate_id": str(gate.id)},
                )
            if review["verdict"] == "CHANGES_REQUIRED":
                task = self.connection.execute(
                    "SELECT * FROM architect_rework_tasks WHERE review_id=?",
                    (review["id"],),
                ).fetchone()
                if task is None:
                    raise ValueError("review rework task was not durably created")
                job = ReviewReworkCoordinator(
                    self.connection, clock=self.clock, id_factory=self.id_factory
                ).enqueue(
                    subject.project_id,
                    subject.milestone_id,
                    review["id"],
                    task["id"],
                    review["pull_request_id"],
                    correlation_id,
                    self.clock(),
                )
                return RecoveryDecision(
                    "ARCHITECT_HANDOFF",
                    RecoveryDisposition.RECONCILED,
                    "persisted rework handoff restored without Architect call",
                    {"review_id": review["id"], "job_id": str(job.id)},
                )
        return None

    def _advance_milestone(
        self,
        subject: RecoverySubject,
        target: MilestoneState,
        correlation_id: str,
        reason: str,
    ) -> None:
        assert subject.milestone_id is not None and subject.milestone_state is not None
        with transaction(self.connection):
            self.milestones.apply_transition(
                MilestoneTransitionRequest(
                    subject.milestone_id,
                    subject.project_id,
                    MilestoneState(subject.milestone_state),
                    target,
                    reason,
                    "RECOVERY",
                    "startup-recovery",
                    correlation_id,
                    self.clock(),
                )
            )

    def _recover_merge(
        self, subject: RecoverySubject, correlation_id: str
    ) -> RecoveryDecision:
        assert subject.milestone_id is not None and self.services.gatekeeper is not None
        row = self.connection.execute(
            """SELECT a.*,r.external_repository_id,p.external_pr_number
               FROM merge_attempts a JOIN pull_requests p ON p.id=a.pull_request_id
               JOIN github_repositories r ON r.id=p.github_repository_id
               WHERE a.project_id=? AND a.milestone_id=?
               ORDER BY a.requested_at DESC,a.id LIMIT 1""",
            (str(subject.project_id), str(subject.milestone_id)),
        ).fetchone()
        if row is None:
            raise ValueError("merge recovery lacks durable merge attempt")
        request = MergeRequest(
            MERGE_INTERFACE_VERSION,
            correlation_id,
            subject.project_id,
            subject.milestone_id,
            row["external_repository_id"],
            row["external_pr_number"],
            row["expected_head_sha"],
            MergeStrategy(row["merge_strategy"]),
            row["gatekeeper_result_id"],
        )
        outcome = self.services.gatekeeper.recover(row["id"], request, now=self.clock())
        if outcome:
            return RecoveryDecision(
                subject.milestone_state or "MERGE",
                RecoveryDisposition.RECONCILED,
                "existing merge observed and independently verified",
                {"merge_attempt_id": row["id"], "status": MergeStatus.MERGED.value},
            )
        return self._block(subject, correlation_id, "merge outcome remains uncertain")

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
        if not clean:
            blocked = self._block(
                subject,
                correlation_id,
                "lost local process has a dirty, missing, or ambiguous workspace",
            )
            return RecoveryDecision(
                job.job_type,
                RecoveryDisposition.BLOCKED,
                blocked.action,
                {
                    "abandoned_job_id": str(abandoned.id),
                    "attempt_number": abandoned.attempt_number,
                    "workspace_clean": False,
                    "worktree_preserved": True,
                },
                blocked.reason,
            )
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
            None,
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
