# ruff: noqa: E501
"""Provider-neutral, state-driven M32 lifecycle coordination.

The coordinator decides *what* durable work is due.  Executors remain responsible
for every external operation and for advancing the state after verified evidence.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from syntra_build.application.implementation_policy import block_implementation
from syntra_build.domain import (
    Job,
    JobId,
    JobState,
    Milestone,
    MilestoneId,
    MilestoneState,
    MilestoneTransitionRequest,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    WorkerClass,
)
from syntra_build.domain.specification import PlannedMilestone
from syntra_build.infrastructure.persistence.connection import transaction_scope
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

ACTIVE_JOB_STATES = (
    "QUEUED",
    "DISPATCHED",
    "RUNNING",
    "WAITING_EXTERNAL",
    "RETRY_WAIT",
)

STATE_JOBS: Mapping[str, tuple[str, WorkerClass]] = {
    "PROVISIONING": ("REPOSITORY_PROVISION", WorkerClass.GITHUB),
    "PREPARING_WORKSPACE": ("WORKSPACE_PREPARE", WorkerClass.GIT),
    "CODING": ("CODEX_RUN", WorkerClass.CODEX),
    "VALIDATING_CHANGES": ("CHANGE_VALIDATE", WorkerClass.GIT),
    "COMMITTING": ("GIT_COMMIT", WorkerClass.GIT),
    "PUSHING": ("GIT_PUSH", WorkerClass.GIT),
    "PR_CREATING": ("PR_CREATE", WorkerClass.GITHUB),
    "ARCHITECT_REVIEW": ("ARCHITECT_REVIEW", WorkerClass.ARCHITECT),
    "MERGE_READY": ("PR_MERGE", WorkerClass.GITHUB),
}


class JobTypeDispatcher:
    """Fail-closed routing for multiple released job types in one worker class."""

    def __init__(self, routes: Mapping[str, Callable[[Job], object]]) -> None:
        self._routes = dict(routes)

    @property
    def job_types(self) -> frozenset[str]:
        """Expose the closed, trusted routing vocabulary for composition checks."""
        return frozenset(self._routes)

    def execute(self, job: Job) -> object:
        executor = self._routes.get(job.job_type)
        if executor is None:
            raise RuntimeError(f"unsupported trusted job type: {job.job_type}")
        return executor(job)

    def __call__(self, job: Job) -> object:
        return self.execute(job)


class LifecycleCoordinator:
    """Inspect persisted truth and atomically enqueue missing lifecycle work."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._db, self._clock, self._ids = connection, clock, id_factory
        self._jobs = SQLiteJobRepository(connection, id_factory)
        self._milestones = SQLiteMilestoneRepository(connection, id_factory)
        self._projects = SQLiteProjectRepository(connection, id_factory)

    def enqueue_due(self) -> int:
        """Make one bounded pass; database uniqueness makes repeated ticks no-ops."""
        count = 0
        for project in self._db.execute(
            "SELECT id,state FROM projects WHERE state NOT IN ('PAUSED','CANCELLED','FAILED','COMPLETE') ORDER BY created_at,id"
        ).fetchall():
            project_id = ProjectId.from_string(project["id"])
            if project["state"] == "DESIGNING":
                # A historical/manual fixture is not generated-project provenance.
                if not self._db.execute(
                    "SELECT 1 FROM project_creation_context WHERE project_id=?",
                    (project["id"],),
                ).fetchone():
                    continue
                mode = self._latest_design_mode(project["id"])
                if mode == "PROPOSE_DESIGN":
                    count += self._enqueue(
                        project_id, None, "SPECIFICATION_DRAFT", WorkerClass.ARCHITECT
                    )
                elif mode != "ASK_USER":
                    count += self._enqueue(
                        project_id, None, "ARCHITECT_DESIGN", WorkerClass.ARCHITECT
                    )
            elif project["state"] == "PROVISIONING":
                if self._approved_package(project["id"]):
                    count += self._enqueue(
                        project_id, None, "REPOSITORY_PROVISION", WorkerClass.GITHUB
                    )
            elif project["state"] == "DESIGN_APPROVAL":
                with transaction_scope(self._db):
                    count += self._design_notification(project_id)
            elif project["state"] == "READY":
                count += self._materialise(project_id)
            elif project["state"] == "BUILDING":
                count += self._building(project_id)
        return count

    def _design_notification(self, project_id: ProjectId) -> int:
        # Discover committed intent, including packages stranded before this route existed.
        # A terminal failure must not reset the bounded notification retry budget each tick.
        row = self._db.execute(
            """SELECT g.id FROM design_packages d
               JOIN human_gates g ON g.id=d.approval_gate_id AND g.project_id=d.project_id
               WHERE d.project_id=? AND d.status='PENDING_APPROVAL'
                 AND g.state='PENDING' AND g.gate_type='DESIGN_APPROVAL'
                 AND NOT EXISTS (
                     SELECT 1 FROM jobs j WHERE j.project_id=d.project_id
                       AND j.job_type='DESIGN_APPROVAL_NOTIFY' AND j.state='FAILED'
                       AND json_extract(j.payload_json,'$.gate_id')=g.id)
               ORDER BY d.created_at,d.id LIMIT 1""",
            (str(project_id),),
        ).fetchone()
        if row is None:
            return 0
        return self._enqueue(
            project_id,
            None,
            "DESIGN_APPROVAL_NOTIFY",
            WorkerClass.MESSAGING,
            {"gate_id": row["id"]},
        )

    def _enqueue(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId | None,
        job_type: str,
        worker: WorkerClass,
        payload: Mapping[str, str] | None = None,
    ) -> int:
        # A state-entry identity scopes automatic work. Terminal jobs consume
        # that entry's budget; only an authoritative transition opens a new one.
        with transaction_scope(self._db):
            return self._enqueue_locked(
                project_id, milestone_id, job_type, worker, payload
            )

    def _enqueue_locked(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId | None,
        job_type: str,
        worker: WorkerClass,
        payload: Mapping[str, str] | None,
    ) -> int:
        if milestone_id and job_type in {"CODEX_RUN", "CHANGE_VALIDATE"}:
            current = self._db.execute(
                "SELECT m.state,p.state AS project_state FROM milestones m JOIN projects p ON p.id=m.project_id WHERE m.id=? AND m.project_id=?",
                (str(milestone_id), str(project_id)),
            ).fetchone()
            expected = "CODING" if job_type == "CODEX_RUN" else "VALIDATING_CHANGES"
            if (
                current is None
                or current["state"] != expected
                or current["project_state"] != "BUILDING"
            ):
                return 0
        scope = "milestone_id=?" if milestone_id else "milestone_id IS NULL"
        parameters: list[object] = [str(project_id), job_type]
        if milestone_id:
            parameters.append(str(milestone_id))
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        if self._db.execute(
            f"SELECT 1 FROM jobs WHERE project_id=? AND job_type=? AND {scope} AND state IN ({placeholders}) LIMIT 1",
            (*parameters, *ACTIVE_JOB_STATES),
        ).fetchone():
            return 0
        if milestone_id and job_type in {"CODEX_RUN", "CHANGE_VALIDATE"}:
            entry = self._db.execute(
                "SELECT id,created_at FROM state_transitions WHERE entity_type='MILESTONE' AND entity_id=? ORDER BY rowid DESC LIMIT 1",
                (str(milestone_id),),
            ).fetchone()
            generation = entry["id"] if entry else str(milestone_id)
            boundary = entry["created_at"] if entry else ""
            terminal = self._db.execute(
                f"SELECT id FROM jobs WHERE project_id=? AND job_type=? AND {scope} AND state IN ('FAILED','ABANDONED','CANCELLED','SUCCEEDED') AND (json_extract(payload_json,'$.lifecycle_generation')=? OR (json_extract(payload_json,'$.lifecycle_generation') IS NULL AND updated_at>=?)) ORDER BY rowid DESC LIMIT 1",
                (*parameters, generation, boundary),
            ).fetchone()
            if terminal:
                failed = self._jobs.get(JobId.from_string(terminal["id"]), project_id)
                block_implementation(
                    self._db,
                    failed,
                    "Terminal implementation job requires operator reconciliation; human intervention required",
                    self._clock(),
                )
                return 0
            payload = {**(payload or {}), "lifecycle_generation": generation}
        now, raw_id = self._clock(), self._ids()
        self._jobs.add(
            Job(
                JobId.from_string(raw_id),
                project_id,
                job_type,
                JobState.QUEUED,
                0,
                now,
                now,
                milestone_id,
                correlation_id=raw_id,
                max_attempts=1 if job_type == "CODEX_RUN" else 3,
                scheduled_at=now,
                worker_class=worker,
                payload=payload,
            )
        )
        return 1

    def _latest_design_mode(self, project_id: str) -> str | None:
        row = self._db.execute(
            """SELECT s.normalised_payload_json FROM architect_requests q JOIN architect_responses s ON s.architect_request_id=q.id WHERE q.project_id=? AND q.request_type='DESIGN' ORDER BY s.created_at DESC,s.id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        return json.loads(row[0]).get("mode") if row else None

    def _approved_package(self, project_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._db.execute(
                "SELECT * FROM design_packages WHERE project_id=? AND status='APPROVED' ORDER BY approved_at DESC,id DESC LIMIT 1",
                (project_id,),
            ).fetchone(),
        )

    def _materialise(self, project_id: ProjectId) -> int:
        package = self._approved_package(str(project_id))
        verified = self._db.execute(
            "SELECT 1 FROM github_repositories WHERE project_id=? AND status='VERIFIED'",
            (str(project_id),),
        ).fetchone()
        if (
            package is None
            or verified is None
            or self._db.execute(
                "SELECT 1 FROM milestones WHERE project_id=?", (str(project_id),)
            ).fetchone()
        ):
            return 0
        planned = tuple(
            PlannedMilestone.from_dict(item)
            for item in json.loads(package["planned_milestones_json"])
        )
        if not planned:
            return 0
        now = self._clock()
        with transaction_scope(self._db):
            previous: MilestoneId | None = None
            for sequence, item in enumerate(planned):
                milestone_id = MilestoneId.generate()
                state = (
                    MilestoneState.READY if sequence == 0 else MilestoneState.PENDING
                )
                self._milestones.add(
                    Milestone(
                        milestone_id,
                        project_id,
                        sequence,
                        item.code,
                        item.title,
                        state,
                        now,
                        now,
                    )
                )
                if previous is not None:
                    self._milestones.add_dependency(milestone_id, previous)
                previous = milestone_id
            self._projects.apply_transition(
                ProjectTransitionRequest(
                    project_id,
                    ProjectState.READY,
                    ProjectState.BUILDING,
                    "approved milestones materialised",
                    "SYSTEM",
                    "lifecycle",
                    str(package["id"]),
                    now,
                )
            )
        return 1

    def _building(self, project_id: ProjectId) -> int:
        rows = self._db.execute(
            "SELECT id,state FROM milestones WHERE project_id=? ORDER BY sequence_number",
            (str(project_id),),
        ).fetchall()
        if (
            rows
            and all(row["state"] == "COMPLETE" for row in rows)
            and self._completion_unblocked(project_id)
        ):
            now = self._clock()
            with transaction_scope(self._db):
                self._projects.apply_transition(
                    ProjectTransitionRequest(
                        project_id,
                        ProjectState.BUILDING,
                        ProjectState.COMPLETING,
                        "all milestones complete",
                        "SYSTEM",
                        "lifecycle",
                        self._ids(),
                        now,
                    )
                )
                self._projects.apply_transition(
                    ProjectTransitionRequest(
                        project_id,
                        ProjectState.COMPLETING,
                        ProjectState.COMPLETE,
                        "completion evidence verified",
                        "SYSTEM",
                        "lifecycle",
                        self._ids(),
                        now,
                    )
                )
            return 1
        ready = next((row for row in rows if row["state"] == "READY"), None)
        if ready:
            milestone_id = MilestoneId.from_string(ready["id"])
            now = self._clock()
            with transaction_scope(self._db):
                self._milestones.apply_transition(
                    MilestoneTransitionRequest(
                        milestone_id,
                        project_id,
                        MilestoneState.READY,
                        MilestoneState.PREPARING_TASK,
                        "implementation task due",
                        "SYSTEM",
                        "lifecycle",
                        self._ids(),
                        now,
                    )
                )
                self._enqueue(
                    project_id,
                    milestone_id,
                    "ARCHITECT_TASK",
                    WorkerClass.ARCHITECT,
                    {"task_type": "IMPLEMENT"},
                )
            return 1
        active = next(
            (
                row
                for row in rows
                if row["state"] not in {"PENDING", "COMPLETE", "FAILED", "CANCELLED"}
            ),
            None,
        )
        if active:
            job = STATE_JOBS.get(active["state"])
            if active["state"] == "CODING" and self._review_rework_owns_coding(
                active["id"]
            ):
                owner = self._db.execute(
                    "SELECT id,state FROM jobs WHERE milestone_id=? AND job_type='CODEX_REVIEW_REWORK' ORDER BY rowid DESC LIMIT 1",
                    (active["id"],),
                ).fetchone()
                if owner is not None and owner["state"] not in ACTIVE_JOB_STATES:
                    blocked_job = self._jobs.get(
                        JobId.from_string(owner["id"]), project_id
                    )
                    block_implementation(
                        self._db,
                        blocked_job,
                        "Terminal Architect review rework requires human intervention",
                        self._clock(),
                    )
                return 0
            return (
                self._enqueue(project_id, MilestoneId.from_string(active["id"]), *job)
                if job
                else 0
            )
        pending = next((row for row in rows if row["state"] == "PENDING"), None)
        if pending:
            milestone_id = MilestoneId.from_string(pending["id"])
            self._milestones.apply_transition(
                MilestoneTransitionRequest(
                    milestone_id,
                    project_id,
                    MilestoneState.PENDING,
                    MilestoneState.READY,
                    "dependencies complete",
                    "SYSTEM",
                    "lifecycle",
                    self._ids(),
                    self._clock(),
                )
            )
            return 1
        return 0

    def _review_rework_owns_coding(self, milestone_id: str) -> bool:
        """Never reinterpret review-rework CODING provenance as initial work."""
        entry = self._db.execute(
            "SELECT actor_id,metadata_json FROM state_transitions WHERE entity_type='MILESTONE' AND entity_id=? AND new_state='CODING' ORDER BY rowid DESC LIMIT 1",
            (milestone_id,),
        ).fetchone()
        if entry is not None and entry["actor_id"] == "implementation-policy":
            validation_id = json.loads(entry["metadata_json"] or "{}").get(
                "validation_id"
            )
            return (
                validation_id is not None
                and self._db.execute(
                    "SELECT 1 FROM jobs WHERE milestone_id=? AND job_type='CODEX_REVIEW_REWORK' AND json_extract(payload_json,'$.validation_id')=?",
                    (milestone_id, validation_id),
                ).fetchone()
                is not None
            )
        if entry is not None and entry["actor_id"] == "syntra-build-admin":
            return False
        return (
            self._db.execute(
                """SELECT 1 FROM jobs WHERE milestone_id=?
               AND job_type='CODEX_REVIEW_REWORK'
               AND state IN ('QUEUED','DISPATCHED','RUNNING','WAITING_EXTERNAL',
                             'RETRY_WAIT','SUCCEEDED','FAILED','CANCELLED',
                             'ABANDONED') LIMIT 1""",
                (milestone_id,),
            ).fetchone()
            is not None
        )

    def _completion_unblocked(self, project_id: ProjectId) -> bool:
        """Require resolved human/security evidence before project completion."""
        unresolved_gate = self._db.execute(
            """SELECT 1 FROM human_gates WHERE project_id=?
               AND state NOT IN ('RESOLVED','EXPIRED','CANCELLED') LIMIT 1""",
            (str(project_id),),
        ).fetchone()
        blocking_security = self._db.execute(
            """SELECT 1 FROM security_events e
               WHERE e.project_id=? AND e.blocking=1
                 AND e.severity IN ('HIGH','CRITICAL')
                 AND NOT EXISTS (SELECT 1 FROM security_event_resolutions r
                                 WHERE r.security_event_id=e.id)
               LIMIT 1""",
            (str(project_id),),
        ).fetchone()
        return unresolved_gate is None and blocking_security is None
