"""M25 deterministic creation and execution of Architect-requested human gates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from syntra_build.application.commands.models import Command
from syntra_build.application.gates import (
    CreateGateRequest,
    GateNotifier,
    HumanGateService,
)
from syntra_build.application.review_rework import ReviewReworkCoordinator
from syntra_build.domain.gate_state_machine import GateTransitionRequest
from syntra_build.domain.gates import (
    ExpectedResponseType,
    GateState,
    GateType,
    HumanGate,
    HumanGateResponse,
    HumanTestResponse,
    validate_response,
)
from syntra_build.domain.identifiers import GateId, MilestoneId, ProjectId
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.project_state_machine import ProjectTransitionRequest
from syntra_build.domain.projects import ProjectState
from syntra_build.domain.reviews import ArchitectReview, ArchitectReviewVerdict
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import (
    ClosedGateError,
    PersistenceError,
)
from syntra_build.infrastructure.persistence.gates import SQLiteHumanGateRepository
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository


class HumanInterventionError(PersistenceError):
    """A human action could not safely mutate the intended workflow."""


class HumanInterventionService:
    """Coordinate M10 gates with exact-head CI/review evidence and workflow state."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        authorised_responder_ids: frozenset[str],
        notifier: GateNotifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.connection, self.notifier, self.clock, self.id_factory = (
            connection,
            notifier,
            clock,
            id_factory,
        )
        self.authorised_responder_ids = authorised_responder_ids
        self.gate_repository = SQLiteHumanGateRepository(connection, id_factory)
        self.gates = HumanGateService(
            self.gate_repository,
            gate_id_factory=lambda: GateId.from_string(id_factory()),
            response_id_factory=id_factory,
            authorised_responder_ids=authorised_responder_ids,
        )
        self.milestones = SQLiteMilestoneRepository(connection, id_factory)
        self.projects = SQLiteProjectRepository(connection, id_factory)

    def create_from_review(
        self,
        review_id: str,
        review: ArchitectReview,
        pull_request_id: str,
        ci_run_id: str,
        *,
        causation_id: str,
        occurred_at: datetime | None = None,
    ) -> HumanGate:
        """Create exactly one correlated gate and enter external-waiting states."""
        existing = self.connection.execute(
            "SELECT id FROM human_gates WHERE architect_review_id=?", (review_id,)
        ).fetchone()
        if existing is not None:
            return self.gate_repository.get(GateId.from_string(existing["id"]))
        detail = review.human_gate
        if detail is None or review.verdict not in {
            ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED,
            ArchitectReviewVerdict.HUMAN_TEST_REQUIRED,
        }:
            raise HumanInterventionError("review does not contain a human gate request")
        now = occurred_at or self.clock()
        is_test = review.verdict is ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
        prompt = detail.test_instructions if is_test else detail.prompt
        if prompt is None:
            raise HumanInterventionError("human test instructions are required")
        gate_type = GateType.HUMAN_TEST if is_test else GateType.TECHNICAL_DECISION
        ci = self.connection.execute(
            """SELECT id,head_sha,overall_status FROM ci_runs
            WHERE id=? AND pull_request_id=? AND head_sha=?""",
            (ci_run_id, pull_request_id, review.reviewed_sha),
        ).fetchone()
        if ci is None or ci["overall_status"] != "PASSED":
            raise HumanInterventionError("gate requires exact-head passing CI evidence")
        with transaction(self.connection):
            gate = self.gates.create(
                CreateGateRequest(
                    review.project_id,
                    gate_type,
                    "Human test required" if is_test else "Human decision required",
                    prompt,
                    ExpectedResponseType.HUMAN_TEST
                    if is_test
                    else ExpectedResponseType.OPTION,
                    now,
                    "ARCHITECT_REVIEW_POLICY",
                    review.correlation_id,
                    review.milestone_id,
                    detail.options,
                    review.summary,
                    resume_milestone_state=detail.resume_milestone_state,
                    artifact_reference=detail.artifact_reference,
                    architect_review_id=review_id,
                    causation_id=causation_id,
                )
            )
            if is_test:
                self.connection.execute(
                    """INSERT INTO human_test_bindings
                    (gate_id,project_id,milestone_id,architect_review_id,pull_request_id,
                     pull_request_number,tested_head_sha,ci_run_id,artifact_reference,
                     test_instructions,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(gate.id),
                        str(review.project_id),
                        str(review.milestone_id),
                        review_id,
                        pull_request_id,
                        review.pull_request_number,
                        review.reviewed_sha,
                        ci_run_id,
                        detail.artifact_reference,
                        detail.test_instructions,
                        now.isoformat(),
                    ),
                )
            self.milestones.apply_transition(
                MilestoneTransitionRequest(
                    review.milestone_id,
                    review.project_id,
                    MilestoneState.ARCHITECT_REVIEW,
                    MilestoneState.HUMAN_TEST
                    if is_test
                    else MilestoneState.HUMAN_DECISION,
                    "Architect verdict requires durable human action",
                    "SYSTEM",
                    "human-intervention",
                    review.correlation_id,
                    now,
                    metadata={"gate_id": str(gate.id), "review_id": review_id},
                )
            )
            project = self.projects.get(review.project_id)
            if project.state is ProjectState.BUILDING:
                self.projects.apply_transition(
                    ProjectTransitionRequest(
                        review.project_id,
                        ProjectState.BUILDING,
                        ProjectState.WAITING_HUMAN,
                        "milestone awaits human action",
                        "SYSTEM",
                        "human-intervention",
                        review.correlation_id,
                        now,
                    )
                )
        if self.notifier is not None:
            gate = self.gates.notify(gate.id, self.notifier, occurred_at=now)
        return gate

    def respond(self, command: Command, gate: HumanGate) -> str:
        """Authenticated Telegram command handler for M25 gates."""
        if command.gate_reference != str(gate.id):
            raise HumanInterventionError("response is not correlated to this gate")
        gate = self.gate_repository.get(gate.id)
        if command.requested_by not in self.authorised_responder_ids:
            raise PermissionError("responder is not authorised")
        if gate.state is not GateState.NOTIFIED:
            raise ClosedGateError("gate is not answerable")
        code = validate_response(gate, command.gate_response or "")
        now = command.requested_at
        response_id = self.id_factory()
        response = HumanGateResponse(
            response_id,
            gate.id,
            command.source_message_id,
            code,
            command.gate_feedback,
            code
            if gate.expected_response_type is ExpectedResponseType.OPTION
            else None,
            (),
            command.requested_by,
            now,
            True,
            "M25 identity and evidence validated",
        )
        target = gate.resume_milestone_state
        if target is None:
            raise HumanInterventionError("gate has no persisted resume target")
        binding = None
        if gate.gate_type is GateType.HUMAN_TEST:
            binding = self._validated_test_binding(gate)
            if code == HumanTestResponse.FAIL:
                target = MilestoneState.REVIEW_REWORK
            elif code == HumanTestResponse.BLOCKED:
                target = MilestoneState.BLOCKED
        with transaction(self.connection):
            responded = self.gate_repository.record_response(
                self._gate_transition(
                    gate, GateState.RESPONDED, now, command, "human response received"
                ),
                response,
            )
            validated = self.gate_repository.apply_transition(
                self._gate_transition(
                    responded,
                    GateState.VALIDATED,
                    now,
                    command,
                    "response and evidence validated",
                )
            )
            self.gate_repository.apply_transition(
                self._gate_transition(
                    validated, GateState.RESOLVED, now, command, "human gate resolved"
                )
            )
            if binding is not None:
                self.connection.execute(
                    "INSERT INTO human_test_results VALUES (?,?,?,?,?,?,?,?)",
                    (
                        self.id_factory(),
                        str(gate.id),
                        response_id,
                        code,
                        command.gate_feedback,
                        binding["tested_head_sha"],
                        binding["ci_run_id"],
                        now.isoformat(),
                    ),
                )
            self.milestones.apply_transition(
                MilestoneTransitionRequest(
                    self._milestone_id(gate),
                    gate.project_id,
                    MilestoneState.HUMAN_TEST
                    if gate.gate_type is GateType.HUMAN_TEST
                    else MilestoneState.HUMAN_DECISION,
                    target,
                    f"human gate resolved as {code}",
                    "HUMAN",
                    command.requested_by,
                    command.correlation_id,
                    now,
                    metadata={"gate_id": str(gate.id), "response_id": response_id},
                )
            )
            project = self.projects.get(gate.project_id)
            project_target = (
                ProjectState.BLOCKED
                if target is MilestoneState.BLOCKED
                else ProjectState.BUILDING
            )
            if project.state is ProjectState.WAITING_HUMAN:
                self.projects.apply_transition(
                    ProjectTransitionRequest(
                        gate.project_id,
                        ProjectState.WAITING_HUMAN,
                        project_target,
                        f"human gate resolved as {code}",
                        "HUMAN",
                        command.requested_by,
                        command.correlation_id,
                        now,
                    )
                )
            if gate.gate_type is GateType.HUMAN_TEST and code == HumanTestResponse.FAIL:
                assert binding is not None and gate.architect_review_id is not None
                task_id = self.id_factory()
                payload = {
                    "task_type": "REVIEW_REWORK",
                    "human_test_gate_id": str(gate.id),
                    "human_response_id": response_id,
                    "human_evidence": command.gate_feedback,
                    "pull_request_number": binding["pull_request_number"],
                    "branch": binding["head_branch"],
                    "reviewed_sha": binding["tested_head_sha"],
                    "findings": [],
                    "agents_instructions": self._agents_document(gate.project_id),
                }
                self.connection.execute(
                    "INSERT INTO architect_rework_tasks VALUES "
                    "(?,?,?,?,?,'REVIEW_REWORK',?,?)",
                    (
                        task_id,
                        gate.architect_review_id,
                        str(gate.project_id),
                        str(self._milestone_id(gate)),
                        binding["pull_request_id"],
                        json.dumps(payload, sort_keys=True),
                        now.isoformat(),
                    ),
                )
                ReviewReworkCoordinator(
                    self.connection, clock=self.clock, id_factory=self.id_factory
                ).enqueue(
                    gate.project_id,
                    self._milestone_id(gate),
                    gate.architect_review_id,
                    task_id,
                    binding["pull_request_id"],
                    command.correlation_id,
                    now,
                )
        return f"Human gate {gate.id} resolved as {code}."

    @staticmethod
    def _milestone_id(gate: HumanGate) -> MilestoneId:
        if gate.milestone_id is None:
            raise HumanInterventionError("M25 gate is not milestone correlated")
        return gate.milestone_id

    def _validated_test_binding(self, gate: HumanGate) -> sqlite3.Row:
        row = self.connection.execute(
            """SELECT b.*,p.head_sha,p.head_branch,p.external_pr_number,
            c.overall_status,c.head_sha AS ci_head
            FROM human_test_bindings b JOIN pull_requests p ON p.id=b.pull_request_id
            JOIN ci_runs c ON c.id=b.ci_run_id
            WHERE b.gate_id=? AND b.project_id=? AND b.milestone_id=?
              AND p.project_id=b.project_id AND p.milestone_id=b.milestone_id
              AND p.state='OPEN'""",
            (str(gate.id), str(gate.project_id), str(gate.milestone_id)),
        ).fetchone()
        if row is None or row["external_pr_number"] != row["pull_request_number"]:
            raise HumanInterventionError("human test PR identity is no longer current")
        if row["head_sha"] != row["tested_head_sha"]:
            raise HumanInterventionError("human test head SHA is stale")
        if (
            row["overall_status"] != "PASSED"
            or row["ci_head"] != row["tested_head_sha"]
        ):
            raise HumanInterventionError(
                "human test CI evidence is stale or not passing"
            )
        return cast(sqlite3.Row, row)

    def _gate_transition(
        self,
        gate: HumanGate,
        target: GateState,
        now: datetime,
        command: Command,
        reason: str,
    ) -> GateTransitionRequest:
        return GateTransitionRequest(
            gate.id,
            gate.project_id,
            gate.state,
            target,
            reason,
            "HUMAN" if target is GateState.RESPONDED else "SYSTEM",
            command.requested_by if target is GateState.RESPONDED else None,
            command.correlation_id,
            now,
            gate.milestone_id,
            f"{command.source_platform}:{command.source_update_id}:{command.source_message_id}",
        )

    def _agents_document(self, project_id: ProjectId) -> dict[str, object]:
        row = self.connection.execute(
            """SELECT revision,content,content_hash FROM project_documents
            WHERE project_id=? AND document_type='AGENTS' AND status='APPROVED'
            ORDER BY revision DESC LIMIT 1""",
            (str(project_id),),
        ).fetchone()
        if row is None:
            raise HumanInterventionError("approved AGENTS document is unavailable")
        return {
            "revision": row["revision"],
            "content": row["content"],
            "hash": row["content_hash"],
        }


class HumanTestFreshness:
    """Reusable M26 seam: current PASS evidence for exact PR/head/CI identity."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def is_current(
        self,
        project_id: ProjectId,
        milestone_id: MilestoneId,
        pull_request_id: str,
        head_sha: str,
    ) -> bool:
        return (
            self.connection.execute(
                """SELECT 1 FROM human_test_results r
            JOIN human_test_bindings b ON b.gate_id=r.gate_id
            JOIN human_gates g ON g.id=b.gate_id
            JOIN pull_requests p ON p.id=b.pull_request_id
            JOIN ci_runs c ON c.id=b.ci_run_id
            WHERE b.project_id=? AND b.milestone_id=? AND b.pull_request_id=?
              AND b.tested_head_sha=? AND r.outcome='PASS' AND r.tested_head_sha=?
              AND p.state='OPEN' AND p.head_sha=? AND c.overall_status='PASSED'
              AND c.head_sha=? AND g.state='RESOLVED' LIMIT 1""",
                (
                    str(project_id),
                    str(milestone_id),
                    pull_request_id,
                    head_sha,
                    head_sha,
                    head_sha,
                    head_sha,
                ),
            ).fetchone()
            is not None
        )
