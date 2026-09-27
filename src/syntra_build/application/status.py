# ruff: noqa: E501
"""Read-only, provider-neutral projections of committed workflow state."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

from syntra_build.application.commands.models import (
    Command,
    CommandType,
    InboundMessage,
)
from syntra_build.application.commands.services import (
    ProjectResolution,
    ProjectSummary,
    ResolutionOutcome,
)
from syntra_build.application.projects import canonicalize_project_name
from syntra_build.domain import Project, ProjectId, ProjectState
from syntra_build.infrastructure.logging import new_correlation_id
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

ACTIVE_PROJECT_STATES = frozenset(
    {
        ProjectState.NEW,
        ProjectState.DESIGNING,
        ProjectState.PROVISIONING,
        ProjectState.READY,
        ProjectState.BUILDING,
        ProjectState.COMPLETING,
    }
)
_ACTIVE_JOB_STATES = (
    "RUNNING",
    "DISPATCHED",
    "WAITING_EXTERNAL",
    "RETRY_WAIT",
    "QUEUED",
)
_TERMINAL_MILESTONES = ("PENDING", "COMPLETE", "FAILED", "CANCELLED")
_OPEN_GATES = ("PENDING", "NOTIFIED", "RESPONDED", "VALIDATED")


@dataclass(frozen=True, slots=True)
class MilestoneStatusProjection:
    id: str
    code: str
    sequence: int
    title: str
    state: str
    activity: str | None


@dataclass(frozen=True, slots=True)
class WorkerActivity:
    worker_class: str
    job_type: str
    state: str
    attempt: int
    max_attempts: int
    started_at: str | None
    next_retry_at: str | None
    failure_classification: str | None
    last_error_id: str | None
    retry_exhausted: bool
    exhaustion_reason: str | None


@dataclass(frozen=True, slots=True)
class PullRequestStatus:
    number: int
    state: str
    branch: str
    head_sha: str
    web_url: str


@dataclass(frozen=True, slots=True)
class CIStatus:
    status: str
    head_sha: str
    required_checks: tuple[str, ...]
    retry_count: int
    next_poll_at: str | None
    failure_classification: str | None
    current: bool


@dataclass(frozen=True, slots=True)
class ArchitectStatus:
    verdict: str
    reviewed_sha: str
    blocking_findings: int
    current: bool


@dataclass(frozen=True, slots=True)
class HumanAction:
    gate_id: str
    gate_type: str
    state: str
    title: str
    prompt: str
    project_name: str
    milestone_code: str | None
    milestone_title: str | None
    waiting_for_feedback: bool = False


@dataclass(frozen=True, slots=True)
class ProjectStatusProjection:
    id: ProjectId
    name: str
    state: ProjectState
    resume_state: ProjectState | None
    activity: str
    milestone: MilestoneStatusProjection | None
    workers: tuple[WorkerActivity, ...]
    branch: str | None
    pull_request: PullRequestStatus | None
    ci: CIStatus | None
    architect: ArchitectStatus | None
    human_actions: tuple[HumanAction, ...]
    latest_error: str | None
    next_action: str


@contextmanager
def _snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """Keep related reads on one SQLite snapshot, including inside outer transactions."""
    connection.execute("SAVEPOINT status_snapshot")
    try:
        yield
    finally:
        connection.execute("RELEASE status_snapshot")


class SQLiteStatusService:
    """Build rich projections using SQLite only; methods perform no writes."""

    def __init__(
        self, connection: sqlite3.Connection, projects: SQLiteProjectRepository
    ):
        self._connection = connection
        self._projects = projects

    def list_projects(self) -> tuple[ProjectSummary, ...]:
        return tuple(
            ProjectSummary(p.id, p.name, p.state) for p in self._projects.list_all()
        )

    def resolve_project(self, reference: str) -> ProjectResolution:
        project: Project | None = None
        try:
            project = self._projects.get(ProjectId.from_string(reference))
        except ValueError, PersistenceError:
            canonical = canonicalize_project_name(reference.strip())
            project = (
                self._projects.find_by_canonical_name(canonical) if canonical else None
            )
        return ProjectResolution(
            ResolutionOutcome.FOUND if project else ResolutionOutcome.NOT_FOUND,
            ProjectSummary(project.id, project.name, project.state)
            if project
            else None,
        )

    def get_project_status(self, project_id: ProjectId) -> ProjectSummary:
        project = self._projects.get(project_id)
        return ProjectSummary(project.id, project.name, project.state)

    def active_projects(self) -> tuple[ProjectStatusProjection, ...]:
        return tuple(
            self.project_status(project.id)
            for project in self._projects.list_all()
            if project.state in ACTIVE_PROJECT_STATES
        )

    def waiting_for_human(self) -> tuple[HumanAction, ...]:
        with _snapshot(self._connection):
            rows = self._connection.execute(
                f"""SELECT g.*,p.name AS project_name,m.code AS milestone_code,
                m.title AS milestone_title,
                EXISTS(SELECT 1 FROM m25_telegram_feedback_interactions f
                  WHERE f.gate_id=g.id AND f.state='WAITING_FEEDBACK') AS feedback
                FROM human_gates g JOIN projects p ON p.id=g.project_id
                LEFT JOIN milestones m ON m.id=g.milestone_id
                WHERE g.state IN ({",".join("?" for _ in _OPEN_GATES)})
                ORDER BY p.name,g.created_at,g.id""",
                _OPEN_GATES,
            ).fetchall()
            return tuple(self._human_action(row) for row in rows)

    def project_status(self, project_id: ProjectId) -> ProjectStatusProjection:
        with _snapshot(self._connection):
            project = self._projects.get(project_id)
            milestone_row = self._connection.execute(
                f"""SELECT * FROM milestones WHERE project_id=?
                ORDER BY state NOT IN ({",".join("?" for _ in _TERMINAL_MILESTONES)}) DESC,
                sequence_number DESC LIMIT 1""",
                (str(project_id), *_TERMINAL_MILESTONES),
            ).fetchone()
            milestone = self._milestone(milestone_row) if milestone_row else None
            jobs = self._jobs(
                str(project_id),
                milestone.id if milestone else None,
                include_terminal=project.state
                in (ProjectState.BLOCKED, ProjectState.FAILED),
            )
            actions = self._project_actions(str(project_id))
            workspace = self._one(
                "SELECT branch_name FROM git_workspaces WHERE project_id=? AND milestone_id=?",
                (str(project_id), milestone.id if milestone else ""),
            )
            pr_row = self._one(
                """SELECT * FROM pull_requests WHERE project_id=? AND milestone_id=?
                ORDER BY state='OPEN' DESC,updated_at DESC LIMIT 1""",
                (str(project_id), milestone.id if milestone else ""),
            )
            pr = self._pr(pr_row) if pr_row else None
            ci = self._ci(milestone.id, pr) if milestone and pr else None
            architect = self._architect(pr_row["id"], pr) if pr_row and pr else None
            activity = self._activity(
                project.state, jobs, actions, milestone, project.activity
            )
            latest_error = self._latest_error(jobs, str(project_id))
            return ProjectStatusProjection(
                project.id,
                project.name,
                project.state,
                project.resume_state,
                activity,
                milestone,
                jobs,
                workspace["branch_name"] if workspace else (pr.branch if pr else None),
                pr,
                ci,
                architect,
                actions,
                latest_error,
                self._next_action(project.state, jobs, actions, milestone, ci),
            )

    def _one(self, sql: str, values: tuple[object, ...]) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None, self._connection.execute(sql, values).fetchone()
        )

    @staticmethod
    def _milestone(row: sqlite3.Row) -> MilestoneStatusProjection:
        return MilestoneStatusProjection(
            row["id"],
            row["code"],
            row["sequence_number"],
            row["title"],
            row["state"],
            row["activity"],
        )

    def _jobs(
        self,
        project_id: str,
        milestone_id: str | None,
        *,
        include_terminal: bool,
    ) -> tuple[WorkerActivity, ...]:
        rows = self._connection.execute(
            f"""SELECT * FROM jobs WHERE project_id=? AND state IN
            ({",".join("?" for _ in _ACTIVE_JOB_STATES)})
            ORDER BY CASE state WHEN 'RUNNING' THEN 0 WHEN 'DISPATCHED' THEN 1
              WHEN 'WAITING_EXTERNAL' THEN 2 WHEN 'RETRY_WAIT' THEN 3 ELSE 4 END,
              updated_at DESC,id DESC""",
            (project_id, *_ACTIVE_JOB_STATES),
        ).fetchall()
        relevant = [
            r
            for r in rows
            if milestone_id is None or r["milestone_id"] in (None, milestone_id)
        ]
        if not relevant and include_terminal:
            latest = self._connection.execute(
                """SELECT * FROM jobs WHERE project_id=?
                AND state IN ('FAILED','ABANDONED')
                ORDER BY updated_at DESC,id DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            relevant = [latest] if latest is not None else []
        return tuple(
            WorkerActivity(
                r["worker_class"],
                r["job_type"],
                r["state"],
                r["attempt_number"],
                r["max_attempts"],
                r["started_at"],
                r["next_retry_at"],
                r["failure_classification"],
                r["last_error_id"],
                bool(r["retry_exhausted"]),
                r["exhaustion_reason"],
            )
            for r in relevant
        )

    def _project_actions(self, project_id: str) -> tuple[HumanAction, ...]:
        rows = self._connection.execute(
            f"""SELECT g.*,p.name AS project_name,m.code AS milestone_code,
            m.title AS milestone_title,
            EXISTS(SELECT 1 FROM m25_telegram_feedback_interactions f
              WHERE f.gate_id=g.id AND f.state='WAITING_FEEDBACK') AS feedback
            FROM human_gates g JOIN projects p ON p.id=g.project_id
            LEFT JOIN milestones m ON m.id=g.milestone_id
            WHERE g.project_id=? AND g.state IN ({",".join("?" for _ in _OPEN_GATES)})
            ORDER BY g.created_at,g.id""",
            (project_id, *_OPEN_GATES),
        ).fetchall()
        return tuple(self._human_action(row) for row in rows)

    @staticmethod
    def _human_action(row: sqlite3.Row) -> HumanAction:
        return HumanAction(
            row["id"],
            row["gate_type"],
            row["state"],
            row["title"],
            row["prompt"],
            row["project_name"],
            row["milestone_code"],
            row["milestone_title"],
            bool(row["feedback"]),
        )

    @staticmethod
    def _pr(row: sqlite3.Row) -> PullRequestStatus:
        return PullRequestStatus(
            row["external_pr_number"],
            row["state"],
            row["head_branch"],
            row["head_sha"],
            row["web_url"],
        )

    def _ci(self, milestone_id: str, pr: PullRequestStatus) -> CIStatus | None:
        row = self._one(
            "SELECT * FROM ci_runs WHERE milestone_id=? ORDER BY last_checked_at DESC,id DESC LIMIT 1",
            (milestone_id,),
        )
        if row is None:
            return None
        checks = self._connection.execute(
            "SELECT name,status,conclusion FROM ci_checks WHERE ci_run_id=? ORDER BY name",
            (row["id"],),
        ).fetchall()
        return CIStatus(
            row["overall_status"],
            row["head_sha"],
            tuple(f"{c['name']}: {c['conclusion'] or c['status']}" for c in checks),
            row["retry_count"],
            row["next_check_at"],
            row["failure_classification"],
            row["head_sha"] == pr.head_sha,
        )

    def _architect(
        self, pull_request_id: str, pr: PullRequestStatus
    ) -> ArchitectStatus | None:
        row = self._one(
            "SELECT * FROM architect_reviews WHERE pull_request_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (pull_request_id,),
        )
        if row is None:
            return None
        count = self._connection.execute(
            "SELECT count(*) FROM architect_review_findings WHERE review_id=? AND status='OPEN' AND severity IN ('major','critical')",
            (row["id"],),
        ).fetchone()[0]
        return ArchitectStatus(
            row["verdict"],
            row["reviewed_sha"],
            count,
            row["reviewed_sha"] == pr.head_sha and row["superseded_at"] is None,
        )

    def _latest_error(
        self, jobs: tuple[WorkerActivity, ...], project_id: str
    ) -> str | None:
        for job in jobs:
            if job.last_error_id or job.exhaustion_reason:
                return job.last_error_id or job.exhaustion_reason
        row = self._one(
            """SELECT reason FROM state_transitions WHERE project_id=? AND new_state IN ('BLOCKED','FAILED')
            ORDER BY created_at DESC,id DESC LIMIT 1""",
            (project_id,),
        )
        return row["reason"] if row else None

    @staticmethod
    def _activity(
        state: ProjectState,
        jobs: tuple[WorkerActivity, ...],
        actions: tuple[HumanAction, ...],
        milestone: MilestoneStatusProjection | None,
        persisted: str | None,
    ) -> str:
        if jobs:
            job = jobs[0]
            if job.worker_class == "CODEX":
                return f"Codex {job.state.lower()} — {job.job_type}"
            return f"{job.worker_class.title()} {job.state.lower()} — {job.job_type}"
        if actions:
            return (
                "Waiting for human feedback"
                if actions[0].waiting_for_feedback
                else f"Waiting for human {actions[0].gate_type.lower().replace('_', ' ')}"
            )
        if state is ProjectState.PAUSED:
            return "Paused; no workers active"
        if state is ProjectState.BLOCKED:
            return "Blocked; no workers active"
        return persisted or (
            f"Milestone {milestone.state.lower().replace('_', ' ')}"
            if milestone
            else "No active work"
        )

    @staticmethod
    def _next_action(
        state: ProjectState,
        jobs: tuple[WorkerActivity, ...],
        actions: tuple[HumanAction, ...],
        milestone: MilestoneStatusProjection | None,
        ci: CIStatus | None,
    ) -> str:
        if jobs:
            job = jobs[0]
            if job.state == "RETRY_WAIT":
                return (
                    f"Retry is scheduled for {job.next_retry_at}."
                    if job.next_retry_at
                    else "Waiting for the scheduled retry."
                )
            if job.worker_class == "CODEX" and job.state in ("RUNNING", "DISPATCHED"):
                return "Waiting for Codex to finish."
            if job.state == "WAITING_EXTERNAL":
                return "Waiting for the external operation."
        if actions:
            action = actions[0]
            return (
                "Waiting for your feedback."
                if action.waiting_for_feedback
                else "Waiting for your test result."
                if action.gate_type == "HUMAN_TEST"
                else "Waiting for your decision."
            )
        if state is ProjectState.PAUSED:
            return "Resume the project to continue."
        if state is ProjectState.BLOCKED:
            return "Resolve the recorded blocker before work can continue."
        if state is ProjectState.FAILED:
            return "No recovery action is currently recorded."
        if state is ProjectState.COMPLETE:
            return "No further action for this project."
        if (
            milestone
            and milestone.state == "CI_RUNNING"
            or ci
            and ci.current
            and ci.status in ("QUEUED", "RUNNING")
        ):
            return "Waiting for required CI checks."
        if milestone and milestone.state == "ARCHITECT_REVIEW":
            return "Waiting for Architect review."
        if milestone and milestone.state == "MERGE_READY":
            return "Awaiting Gatekeeper merge processing."
        return "Syntra will continue from the persisted workflow state."


class ReadOnlyStatusIntentResolver:
    """Recognise a deliberately small status-only natural-language vocabulary."""

    _PROJECT_PATTERNS = (
        re.compile(r"what(?:'s| is) happening with (.+?)\??$", re.IGNORECASE),
        re.compile(r"what(?:'s| is) the status of (.+?)\??$", re.IGNORECASE),
        re.compile(r"status of (.+?)\??$", re.IGNORECASE),
        re.compile(r"how is (.+?) going\??$", re.IGNORECASE),
    )

    def resolve(self, message: InboundMessage) -> Command | None:
        text = " ".join(message.text.strip().split())
        folded = text.casefold()
        kind: CommandType | None = None
        reference: str | None = None
        if folded in {"what projects are active?", "which projects are active?"}:
            kind = CommandType.ACTIVE_PROJECTS
        elif folded in {
            "is anything waiting for me?",
            "what is waiting for me?",
            "what's waiting for me?",
        }:
            kind = CommandType.WAITING
        else:
            for pattern in self._PROJECT_PATTERNS:
                match = pattern.fullmatch(text)
                if match:
                    kind, reference = CommandType.PROJECT_STATUS, match.group(1).strip()
                    break
        if kind is None:
            return None
        return Command(
            type=kind,
            requested_by=message.sender_id,
            requested_at=message.received_at,
            source_platform=message.source_platform,
            source_update_id=message.source_update_id,
            source_message_id=message.source_message_id,
            correlation_id=new_correlation_id(),
            project_reference=reference,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
        )


def format_project_status(status: ProjectStatusProjection) -> str:
    lines = [
        f"Project: {status.name}",
        f"State: {status.state.value}",
        f"Activity: {status.activity}",
    ]
    if status.resume_state:
        lines.append(f"Resume state: {status.resume_state.value}")
    if status.milestone:
        lines += [
            f"Milestone: {status.milestone.code} — {status.milestone.title}",
            f"Milestone state: {status.milestone.state}",
        ]
    for worker in status.workers:
        detail = f"{worker.worker_class} {worker.state} — {worker.job_type}, attempt {worker.attempt}/{worker.max_attempts}"
        if worker.next_retry_at:
            detail += f", next retry {worker.next_retry_at}"
        if worker.failure_classification:
            detail += f", {worker.failure_classification}"
        lines.append(f"Worker: {detail}")
    if status.branch:
        lines.append(f"Branch: {status.branch}")
    if status.pull_request:
        lines += [
            f"PR: #{status.pull_request.number} {status.pull_request.state}",
            f"Head: {status.pull_request.head_sha}",
        ]
    if status.ci:
        freshness = "" if status.ci.current else " (stale; not current head)"
        lines.append(f"CI: {status.ci.status} @ {status.ci.head_sha[:10]}{freshness}")
        lines.extend(f"  {check}" for check in status.ci.required_checks)
    if status.architect:
        freshness = "" if status.architect.current else " (stale; not current head)"
        lines.append(
            f"Architect: {status.architect.verdict} @ {status.architect.reviewed_sha[:10]}{freshness}; blocking findings: {status.architect.blocking_findings}"
        )
    if status.human_actions:
        for action in status.human_actions:
            request = (
                "feedback required" if action.waiting_for_feedback else action.prompt
            )
            lines.append(
                f"Human action: {action.gate_type} {action.state} — {action.title}: {request} (gate {action.gate_id})"
            )
    else:
        lines.append("Human action: none")
    lines.append(f"Latest error: {status.latest_error or 'none'}")
    lines.append(f"Next: {status.next_action}")
    return "\n".join(lines)


def format_active_projects(projects: tuple[ProjectStatusProjection, ...]) -> str:
    if not projects:
        return "No projects are currently active."
    lines = ["Active projects:"]
    for project in projects:
        milestone = (
            f" — {project.milestone.code} {project.milestone.state}"
            if project.milestone
            else ""
        )
        lines.append(
            f"{project.name} — {project.state.value}{milestone} — {project.activity}"
        )
    return "\n".join(lines)


def format_waiting(actions: tuple[HumanAction, ...]) -> str:
    if not actions:
        return "Nothing is currently waiting for you."
    lines = ["Waiting for you:"]
    for action in actions:
        milestone = (
            f" — {action.milestone_code} {action.milestone_title}"
            if action.milestone_code
            else ""
        )
        request = "feedback required" if action.waiting_for_feedback else action.prompt
        lines.append(
            f"{action.project_name}{milestone} — {action.gate_type} {action.state}: {request} (gate {action.gate_id})"
        )
    return "\n".join(lines)
