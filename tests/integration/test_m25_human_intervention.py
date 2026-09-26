# ruff: noqa: E501
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from syntra_build.adapters.telegram.application import route_authorized_message
from syntra_build.adapters.telegram.client import TelegramClient
from syntra_build.adapters.telegram.gates import TelegramGateNotifier
from syntra_build.adapters.telegram.human_intervention import (
    TelegramHumanInterventionHandler,
    human_gate_callback_data,
)
from syntra_build.adapters.telegram.models import (
    TelegramCallbackQuery,
    TelegramInboundMessage,
    TelegramSentMessage,
)
from syntra_build.application.architect_review import ArchitectReviewService
from syntra_build.application.commands.models import Command, CommandType
from syntra_build.application.human_intervention import (
    HumanInterventionError,
    HumanInterventionService,
    HumanTestFreshness,
)
from syntra_build.domain.design import ARCHITECT_INTERFACE_VERSION
from syntra_build.domain.gates import GateState
from syntra_build.domain.identifiers import GateId, MilestoneId, ProjectId
from syntra_build.domain.milestone_state_machine import MilestoneTransitionRequest
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.domain.reviews import (
    ArchitectHumanGateRequest,
    ArchitectReview,
    ArchitectReviewVerdict,
    HumanDecisionKind,
)
from syntra_build.infrastructure.config import SecretInputs, SecretValue, load_config
from syntra_build.infrastructure.persistence.connection import (
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.errors import ClosedGateError
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
)
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)
from syntra_build.infrastructure.persistence.telegram_interactions import (
    SQLiteM25TelegramFeedbackRepository,
    SQLiteTelegramGateNotificationRepository,
    TelegramInteractionState,
)
from syntra_build.smoke import build_host_router

NOW = datetime(2026, 9, 23, tzinfo=UTC)
SHA_A, SHA_B = "a" * 40, "b" * 40


class Notifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def send(self, text: str) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("telegram unavailable")
        return "telegram-message"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.acknowledged: list[str] = []
        self.next_message_id = 201

    def send_text(
        self,
        *,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> TelegramSentMessage:
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "thread_id": thread_id,
                "reply_to_message_id": reply_to_message_id,
                "reply_markup": reply_markup,
            }
        )
        return TelegramSentMessage(self.next_message_id, chat_id, thread_id)

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        self.acknowledged.append(callback_query_id)


def callback(
    gate_id: GateId, data: str, *, user: int = 42, chat: int = 300, message: int = 201
) -> TelegramCallbackQuery:
    return TelegramCallbackQuery(900, "callback-1", user, chat, message, data, NOW, 7)


def callback_handler(
    db: sqlite3.Connection,
    service: HumanInterventionService,
    fake: FakeTelegram,
) -> TelegramHumanInterventionHandler:
    return TelegramHumanInterventionHandler(
        db, cast(TelegramClient, fake), frozenset({"42"}), service
    )


def bind_notification(db: sqlite3.Connection, gate_id: GateId) -> None:
    SQLiteTelegramGateNotificationRepository(db).add(gate_id, "300", "7", "201", NOW)


def fixture(
    tmp_path: Path, verdict: ArchitectReviewVerdict
) -> tuple[sqlite3.Connection, HumanInterventionService, ArchitectReview, str, str]:
    db = open_database(tmp_path / f"{uuid4()}.db")
    apply_migrations(db)
    project, milestone = ProjectId.generate(), MilestoneId.generate()
    repo, pr_id, request_id, response_id, review_id, ci_id = (
        str(uuid4()) for _ in range(6)
    )
    detail = ArchitectHumanGateRequest(
        "Choose implementation",
        MilestoneState.ARCHITECT_REVIEW,
        ("A", "B") if verdict is ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED else (),
        None
        if verdict is ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED
        else "Exercise feature and report",
        "artifact://build-25",
        HumanDecisionKind.TECHNICAL
        if verdict is ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED
        else None,
    )
    review = ArchitectReview(
        ARCHITECT_INTERFACE_VERSION,
        "corr",
        project,
        milestone,
        25,
        SHA_A,
        verdict,
        "human needed",
        (),
        detail,
    )
    stamp = NOW.isoformat()
    db.execute(
        "INSERT INTO projects (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES (?,?,'BUILDING',?,?,?,?, 'public')",
        (str(project), "M25", stamp, stamp, stamp, f"m25-{uuid4().hex}"),
    )
    db.execute(
        "INSERT INTO project_documents (id,project_id,document_type,revision,status,content,content_hash,created_at,created_by,approved_at,approved_by) VALUES (?,?, 'SPEC',1,'APPROVED','# spec',?,?,'human',?,'human')",
        (str(uuid4()), str(project), "e" * 64, stamp, stamp),
    )
    db.execute(
        "INSERT INTO milestones (id,project_id,sequence_number,code,title,state,created_at,updated_at,definition_json,automated_acceptance_json) VALUES (?,?,25,'M25','Human','ARCHITECT_REVIEW',?,?,'{}','[]')",
        (str(milestone), str(project), stamp, stamp),
    )
    db.execute(
        "INSERT INTO github_repositories (id,project_id,provider,owner,repository_name,full_name,external_repository_id,visibility,default_branch,status,created_at,updated_at,verified_at) VALUES (?,?,'github','owner','repo','owner/repo',77,'public','main','VERIFIED',?,?,?)",
        (repo, str(project), stamp, stamp, stamp),
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        project,
        milestone,
        77,
        25,
        PullRequestState.OPEN,
        "syntra/m25",
        "main",
        SHA_A,
        "https://example/pr/25",
    )
    with transaction(db):
        pr = SQLitePullRequestRepository(db).save_verified(
            pr_id, repo, descriptor, "M25", NOW
        )
    db.execute(
        "INSERT INTO ci_runs (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,overall_status,started_at,completed_at,last_checked_at,summary_json,retry_count) VALUES (?,?,?,?,?,1,'PASSED',?,?,?,'{}',0)",
        (ci_id, str(project), str(milestone), pr.id, SHA_A, stamp, stamp, stamp),
    )
    db.execute(
        "INSERT INTO architect_requests (id,project_id,milestone_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status) VALUES (?,?,?,'REVIEW','fake','model','high','1.0',?,'corr',?,?,'SUCCEEDED')",
        (
            request_id,
            str(project),
            str(milestone),
            json.dumps({"ci_result": {"run_id": ci_id}}),
            stamp,
            stamp,
        ),
    )
    db.execute(
        "INSERT INTO architect_responses (id,architect_request_id,response_type,response_schema_version,normalised_payload_json,status,created_at,validation_status,provider,model) VALUES (?,?,'REVIEW','1.0',?,'ACCEPTED',?,'VALID','fake','model')",
        (response_id, request_id, json.dumps(review.to_dict()), stamp),
    )
    db.execute(
        "INSERT INTO architect_reviews VALUES (?,?,?,?,?,?,?,'reviewed',?,NULL)",
        (
            review_id,
            str(project),
            str(milestone),
            request_id,
            pr.id,
            SHA_A,
            verdict.value,
            stamp,
        ),
    )
    db.execute(
        "INSERT INTO project_documents (id,project_id,document_type,revision,status,content,content_hash,created_at,created_by,approved_at,approved_by) VALUES (?,?, 'AGENTS',1,'APPROVED','# agents',?,?,'human',?,'human')",
        (str(uuid4()), str(project), "f" * 64, stamp, stamp),
    )
    service = HumanInterventionService(
        db,
        authorised_responder_ids=frozenset({"42"}),
        notifier=Notifier(),
        clock=lambda: NOW,
    )
    return db, service, review, pr.id, ci_id


class ReviewGateway:
    def __init__(self, descriptor: PullRequestDescriptor) -> None:
        self.descriptor = descriptor

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        return self.descriptor

    def diff(
        self,
        repository_full_name: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> str:
        return "+m25"


class HumanTestArchitect:
    provider_name = "fake"
    model = "test"

    def review(self, request: object) -> ArchitectReview:
        from syntra_build.domain.reviews import ArchitectReviewRequest

        assert isinstance(request, ArchitectReviewRequest)
        return ArchitectReview(
            ARCHITECT_INTERFACE_VERSION,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.pull_request_number,
            request.head_sha,
            ArchitectReviewVerdict.HUMAN_TEST_REQUIRED,
            "test it",
            (),
            ArchitectHumanGateRequest(
                "test",
                MilestoneState.ARCHITECT_REVIEW,
                (),
                "Exercise the build",
                "artifact://cross-component",
                None,
            ),
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": "m25-response"}


class ApprovalArchitect:
    provider_name = "fake"
    model = "test"

    def __init__(self) -> None:
        self.request: object | None = None

    def review(self, request: object) -> ArchitectReview:
        from syntra_build.domain.reviews import ArchitectReviewRequest

        assert isinstance(request, ArchitectReviewRequest)
        self.request = request
        return ArchitectReview(
            ARCHITECT_INTERFACE_VERSION,
            request.correlation_id,
            request.project_id,
            request.milestone_id,
            request.pull_request_number,
            request.head_sha,
            ArchitectReviewVerdict.APPROVE,
            "approved after decision",
            (),
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {}


class MustNotRunArchitect:
    provider_name = "fake"
    model = "test"

    def review(self, request: object) -> ArchitectReview:
        raise AssertionError("completed Architect review must not be rerun")

    def telemetry(self) -> dict[str, int | str | None]:
        return {}


def command(
    gate_id: str, outcome: str, *, sender: str = "42", message: str = "msg"
) -> Command:
    return Command(
        CommandType.RESPOND_GATE,
        sender,
        NOW,
        "telegram",
        "update",
        message,
        "response",
        gate_reference=gate_id,
        gate_response=outcome,
        gate_feedback="human evidence",
    )


def test_ci_architect_human_test_pass_cross_component(tmp_path: Path) -> None:
    db, interventions, review, pr_id, _ = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        review.project_id,
        review.milestone_id,
        77,
        25,
        PullRequestState.OPEN,
        "syntra/m25",
        "main",
        SHA_A,
        "https://example/pr/25",
    )
    gateway = ReviewGateway(descriptor)
    record = ArchitectReviewService(
        db,
        gateway,
        gateway,
        HumanTestArchitect(),
        clock=lambda: NOW,
        human_interventions=interventions,
    ).review(review.project_id, review.milestone_id, "cross-component")
    gate = interventions.gate_repository.get(
        GateId.from_string(
            db.execute(
                "SELECT id FROM human_gates WHERE architect_review_id=?", (record.id,)
            ).fetchone()[0]
        )
    )
    assert gate.state is GateState.NOTIFIED
    interventions.respond(command(str(gate.id), "PASS"), gate)
    assert (
        db.execute("SELECT state FROM milestones").fetchone()[0] == "ARCHITECT_REVIEW"
    )
    assert (
        db.execute(
            "SELECT count(*) FROM architect_reviews WHERE verdict='APPROVE'"
        ).fetchone()[0]
        == 0
    )
    assert HumanTestFreshness(db).is_current(
        review.project_id, review.milestone_id, pr_id, SHA_A
    )


def test_human_test_gate_exact_binding_pass_and_staleness(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="request",
        occurred_at=NOW,
    )
    assert (
        service.create_from_review(
            gate.architect_review_id or "",
            review,
            pr_id,
            ci_id,
            causation_id="request",
            occurred_at=NOW,
        ).id
        == gate.id
    )
    binding = db.execute("SELECT * FROM human_test_bindings").fetchone()
    assert (
        binding["pull_request_number"],
        binding["tested_head_sha"],
        binding["ci_run_id"],
        binding["artifact_reference"],
    ) == (25, SHA_A, ci_id, "artifact://build-25")
    assert binding["test_instructions"] == "Exercise feature and report"
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "HUMAN_TEST"
    assert db.execute("SELECT state FROM projects").fetchone()[0] == "WAITING_HUMAN"
    result = service.respond(command(str(gate.id), "PASS"), gate)
    assert "PASS" in result
    assert HumanTestFreshness(db).is_current(
        review.project_id, review.milestone_id, pr_id, SHA_A
    )
    assert tuple(
        db.execute("SELECT outcome,evidence_text FROM human_test_results").fetchone()
    ) == ("PASS", "human evidence")
    db.execute(
        "UPDATE pull_requests SET head_sha=?,updated_at=? WHERE id=?",
        (SHA_B, NOW.isoformat(), pr_id),
    )
    assert not HumanTestFreshness(db).is_current(
        review.project_id, review.milestone_id, pr_id, SHA_A
    )
    assert db.execute("SELECT outcome FROM human_test_results").fetchone()[0] == "PASS"


def test_persisted_review_handoff_retry_and_notification_reconciliation(
    tmp_path: Path,
) -> None:
    db, service, review, _, _ = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    review_id = db.execute("SELECT id FROM architect_reviews").fetchone()[0]
    notifier = Notifier(fail=True)
    service.notifier = notifier
    gateway = ReviewGateway(
        PullRequestDescriptor(
            "1.0",
            review.project_id,
            review.milestone_id,
            77,
            25,
            PullRequestState.OPEN,
            "syntra/m25",
            "main",
            SHA_A,
            "https://example/pr/25",
        )
    )
    review_service = ArchitectReviewService(
        db,
        gateway,
        gateway,
        MustNotRunArchitect(),
        clock=lambda: NOW,
        human_interventions=service,
    )
    with pytest.raises(RuntimeError, match="telegram unavailable"):
        review_service.review(review.project_id, review.milestone_id, "corr")
    assert db.execute("SELECT count(*) FROM human_gates").fetchone()[0] == 1
    gate = service.gate_repository.outstanding()[0]
    assert gate.state is GateState.PENDING

    notifier.fail = False
    notified_record = review_service.review(
        review.project_id, review.milestone_id, "corr"
    )
    assert notified_record.id == review_id
    notified = service.gate_repository.get(gate.id)
    assert notified.id == gate.id
    assert notified.state is GateState.NOTIFIED
    assert notifier.calls == 2
    again = review_service.review(review.project_id, review.milestone_id, "corr")
    assert again.id == review_id
    assert notifier.calls == 2
    assert db.execute("SELECT count(*) FROM human_gates").fetchone()[0] == 1


def test_stale_unauthorised_and_unrelated_responses_are_rejected(
    tmp_path: Path,
) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    review_id = db.execute("SELECT id FROM architect_reviews").fetchone()[0]
    gate = service.create_from_review(
        review_id, review, pr_id, ci_id, causation_id="request", occurred_at=NOW
    )
    with pytest.raises(PermissionError):
        service.respond(command(str(gate.id), "PASS", sender="99"), gate)
    db.execute(
        "UPDATE pull_requests SET head_sha=?,updated_at=? WHERE id=?",
        (SHA_B, NOW.isoformat(), pr_id),
    )
    with pytest.raises(HumanInterventionError, match="stale"):
        service.respond(command(str(gate.id), "PASS"), gate)
    assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    assert db.execute("SELECT count(*) FROM human_gate_responses").fetchone()[0] == 0


def test_real_telegram_router_composition_handles_only_authorised_gate_response(
    tmp_path: Path,
) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    review_id = db.execute("SELECT id FROM architect_reviews").fetchone()[0]
    gate = service.create_from_review(
        review_id, review, pr_id, ci_id, causation_id="request", occurred_at=NOW
    )
    config = load_config(
        {"telegram": {"enabled": True, "authorised_user_ids": [42]}},
        environ={},
        secrets=SecretInputs(telegram_bot_token=SecretValue("synthetic-token")),
    )
    router = build_host_router(config, db)
    rejected = route_authorized_message(
        TelegramInboundMessage(1, 10, 5, 99, f"gate {gate.id} PASS", NOW),
        router,
    )
    assert "not authorised" in rejected.text
    assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    accepted = route_authorized_message(
        TelegramInboundMessage(2, 11, 5, 42, f"gate {gate.id} PASS", NOW),
        router,
    )
    assert "resolved as PASS" in accepted.text
    assert service.gate_repository.get(gate.id).state is GateState.RESOLVED


def test_decision_uses_persisted_resume_target(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED
    )
    review_id = db.execute("SELECT id FROM architect_reviews").fetchone()[0]
    gate = service.create_from_review(
        review_id, review, pr_id, ci_id, causation_id="request", occurred_at=NOW
    )
    assert gate.options == ("A", "B")
    service.respond(command(str(gate.id), "A"), gate)
    assert (
        db.execute("SELECT state FROM milestones").fetchone()[0] == "ARCHITECT_REVIEW"
    )
    assert (
        db.execute(
            "SELECT count(*) FROM architect_reviews WHERE verdict='APPROVE'"
        ).fetchone()[0]
        == 0
    )
    response = db.execute(
        "SELECT selected_option,response_text FROM human_gate_responses"
    ).fetchone()
    assert tuple(response) == ("A", "human evidence")
    descriptor = PullRequestDescriptor(
        "1.0",
        review.project_id,
        review.milestone_id,
        77,
        25,
        PullRequestState.OPEN,
        "syntra/m25",
        "main",
        SHA_A,
        "https://example/pr/25",
    )
    architect = ApprovalArchitect()
    gateway = ReviewGateway(descriptor)
    ArchitectReviewService(db, gateway, gateway, architect, clock=lambda: NOW).review(
        review.project_id, review.milestone_id, "after-decision"
    )
    from syntra_build.domain.reviews import ArchitectReviewRequest

    assert isinstance(architect.request, ArchitectReviewRequest)
    assert architect.request.human_decisions == (
        {
            "gate_id": str(gate.id),
            "prompt": "Choose implementation",
            "selected_option": "A",
            "human_feedback": "human evidence",
            "decision_type": "TECHNICAL_DECISION",
            "originating_architect_review_id": review_id,
        },
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (HumanDecisionKind.PRODUCT, "PRODUCT_DECISION"),
        (HumanDecisionKind.TECHNICAL, "TECHNICAL_DECISION"),
    ],
)
def test_decision_kind_maps_to_gate_type(
    tmp_path: Path, kind: HumanDecisionKind, expected: str
) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED
    )
    assert review.human_gate is not None
    review = replace(review, human_gate=replace(review.human_gate, decision_kind=kind))
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="request",
        occurred_at=NOW,
    )
    assert gate.gate_type.value == expected


def test_fail_queues_same_pr_rework_and_blocked_is_distinct(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    review_id = db.execute("SELECT id FROM architect_reviews").fetchone()[0]
    gate = service.create_from_review(
        review_id, review, pr_id, ci_id, causation_id="request", occurred_at=NOW
    )
    service.respond(command(str(gate.id), "FAIL"), gate)
    result = db.execute(
        "SELECT outcome,evidence_text FROM human_test_results"
    ).fetchone()
    assert tuple(result) == ("FAIL", "human evidence")
    task = db.execute(
        "SELECT pull_request_id,task_payload_json FROM architect_rework_tasks"
    ).fetchone()
    assert task["pull_request_id"] == pr_id
    assert '"human_evidence": "human evidence"' in task["task_payload_json"]
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "CODING"
    job = db.execute("SELECT worker_class,payload_json FROM jobs").fetchone()
    assert job["worker_class"] == "CODEX"
    assert pr_id in job["payload_json"]
    with pytest.raises(ClosedGateError):
        service.respond(command(str(gate.id), "FAIL", message="duplicate"), gate)

    db2, service2, review2, pr2, ci2 = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    review_id2 = db2.execute("SELECT id FROM architect_reviews").fetchone()[0]
    gate2 = service2.create_from_review(
        review_id2, review2, pr2, ci2, causation_id="request", occurred_at=NOW
    )
    service2.respond(command(str(gate2.id), "BLOCKED"), gate2)
    assert (
        db2.execute("SELECT outcome FROM human_test_results").fetchone()[0] == "BLOCKED"
    )
    assert db2.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    assert db2.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"


def test_fail_rework_same_pr_sha_b_gets_fresh_human_test_gate(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_a = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    review_id = db.execute("SELECT id FROM architect_reviews").fetchone()[0]
    gate_a = service.create_from_review(
        review_id, review, pr_id, ci_a, causation_id="request-a", occurred_at=NOW
    )
    service.respond(command(str(gate_a.id), "FAIL"), gate_a)
    milestones = SQLiteMilestoneRepository(db, lambda: str(uuid4()))
    state = MilestoneState.CODING
    for target in (
        MilestoneState.VALIDATING_CHANGES,
        MilestoneState.COMMITTING,
        MilestoneState.PUSHING,
        MilestoneState.CI_RUNNING,
        MilestoneState.ARCHITECT_REVIEW,
    ):
        milestones.apply_transition(
            MilestoneTransitionRequest(
                review.milestone_id,
                review.project_id,
                state,
                target,
                "test rework progression",
                "SYSTEM",
                "test",
                "sha-b",
                NOW,
            )
        )
        state = target
    db.execute(
        "UPDATE pull_requests SET head_sha=?,updated_at=?,last_reconciled_at=? WHERE id=?",
        (SHA_B, NOW.isoformat(), NOW.isoformat(), pr_id),
    )
    ci_b = str(uuid4())
    db.execute(
        "INSERT INTO ci_runs (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,overall_status,started_at,completed_at,last_checked_at,summary_json,retry_count) VALUES (?,?,?,?,?,2,'PASSED',?,?,?,'{}',0)",
        (
            ci_b,
            str(review.project_id),
            str(review.milestone_id),
            pr_id,
            SHA_B,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    descriptor = PullRequestDescriptor(
        "1.0",
        review.project_id,
        review.milestone_id,
        77,
        25,
        PullRequestState.OPEN,
        "syntra/m25",
        "main",
        SHA_B,
        "https://example/pr/25",
    )
    gateway = ReviewGateway(descriptor)
    record_b = ArchitectReviewService(
        db,
        gateway,
        gateway,
        HumanTestArchitect(),
        clock=lambda: NOW,
        human_interventions=service,
    ).review(review.project_id, review.milestone_id, "review-b")
    binding_b = db.execute(
        "SELECT * FROM human_test_bindings WHERE architect_review_id=?", (record_b.id,)
    ).fetchone()
    assert binding_b["pull_request_id"] == pr_id
    assert binding_b["tested_head_sha"] == SHA_B
    assert binding_b["ci_run_id"] == ci_b
    assert tuple(
        db.execute(
            "SELECT tested_head_sha,outcome FROM human_test_results WHERE gate_id=?",
            (str(gate_a.id),),
        ).fetchone()
    ) == (SHA_A, "FAIL")
    assert db.execute("SELECT count(*) FROM human_test_bindings").fetchone()[0] == 2


def test_human_test_notification_renders_bounded_inline_actions(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="buttons",
        occurred_at=NOW,
    )
    fake = FakeTelegram()
    notifier = TelegramGateNotifier(
        cast(TelegramClient, fake),
        300,
        7,
        notifications=SQLiteTelegramGateNotificationRepository(db),
        clock=lambda: NOW,
    )
    notifier.send(f"Human test\nGate: {gate.id}")
    notifier.send(f"Human test\nGate: {gate.id}")
    assert len(fake.sent) == 1
    keyboard = fake.sent[0]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    buttons = [row[0] for row in keyboard]
    assert [button["text"] for button in buttons] == ["PASS", "FAIL", "BLOCKED"]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)


def test_pass_inline_callback_resolves_immediately_without_feedback(
    tmp_path: Path,
) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="callback",
        occurred_at=NOW,
    )
    bind_notification(db, gate.id)
    fake = FakeTelegram()
    handled = callback_handler(db, service, fake).handle_callback(
        callback(gate.id, human_gate_callback_data("pass", gate.id))
    )
    assert handled and fake.acknowledged == ["callback-1"]
    assert service.gate_repository.get(gate.id).state is GateState.RESOLVED
    assert db.execute("SELECT outcome FROM human_test_results").fetchone()[0] == "PASS"
    assert (
        db.execute("SELECT evidence_text FROM human_test_results").fetchone()[0] is None
    )
    assert (
        db.execute("SELECT state FROM milestones").fetchone()[0] == "ARCHITECT_REVIEW"
    )


def test_fail_inline_callback_uses_same_pr_rework(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="fail-button",
        occurred_at=NOW,
    )
    bind_notification(db, gate.id)
    fake = FakeTelegram()
    fake.next_message_id = 501
    handler = callback_handler(db, service, fake)
    handler.handle_callback(
        callback(gate.id, human_gate_callback_data("fail", gate.id))
    )
    assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    assert db.execute("SELECT count(*) FROM human_test_results").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    interaction = SQLiteM25TelegramFeedbackRepository(db).active(gate.id)
    assert interaction is not None
    assert interaction.state is TelegramInteractionState.WAITING_FEEDBACK
    assert interaction.prompt_message_id == "501"
    assert fake.sent[-1]["reply_markup"] == {"force_reply": True, "selective": True}
    evidence = "Expected save to succeed; the screen showed error E42."
    assert handler.handle_feedback_reply(
        TelegramInboundMessage(901, 502, 300, 42, evidence, NOW, 7, 501)
    )
    result = db.execute(
        "SELECT outcome,evidence_text FROM human_test_results"
    ).fetchone()
    assert tuple(result) == ("FAIL", evidence)
    assert (
        db.execute("SELECT pull_request_id FROM architect_rework_tasks").fetchone()[0]
        == pr_id
    )
    payload = db.execute(
        "SELECT task_payload_json FROM architect_rework_tasks"
    ).fetchone()[0]
    assert json.loads(payload)["human_evidence"] == evidence
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "CODING"


def test_fail_feedback_is_durable_correlated_and_duplicate_safe(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="durable-feedback",
        occurred_at=NOW,
    )
    bind_notification(db, gate.id)
    fake = FakeTelegram()
    fake.next_message_id = 501
    callback_handler(db, service, fake).handle_callback(
        callback(gate.id, human_gate_callback_data("fail", gate.id))
    )
    # Reconstruct both repository and handler as a process restart would.
    reconstructed = callback_handler(
        db,
        HumanInterventionService(db, authorised_responder_ids=frozenset({"42"})),
        fake,
    )
    for unrelated in (
        TelegramInboundMessage(910, 510, 300, 42, "unrelated", NOW, 7, None),
        TelegramInboundMessage(911, 511, 300, 99, "wrong user", NOW, 7, 501),
        TelegramInboundMessage(912, 512, 999, 42, "wrong chat", NOW, 7, 501),
        TelegramInboundMessage(913, 513, 300, 42, "wrong thread", NOW, 8, 501),
        TelegramInboundMessage(914, 514, 300, 42, "wrong message", NOW, 7, 999),
    ):
        assert not reconstructed.handle_feedback_reply(unrelated)
    empty = TelegramInboundMessage(915, 515, 300, 42, "   ", NOW, 7, 501)
    assert reconstructed.handle_feedback_reply(empty)
    assert SQLiteM25TelegramFeedbackRepository(db).active(gate.id) is not None
    assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    evidence = "The generated report omitted the final totals row."
    reply = TelegramInboundMessage(916, 516, 300, 42, evidence, NOW, 7, 501)
    assert reconstructed.handle_feedback_reply(reply)
    assert not reconstructed.handle_feedback_reply(reply)
    assert db.execute("SELECT count(*) FROM human_gate_responses").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM architect_rework_tasks").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_blocked_waits_for_reason_then_blocks_without_codex(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="blocked-feedback",
        occurred_at=NOW,
    )
    bind_notification(db, gate.id)
    fake = FakeTelegram()
    fake.next_message_id = 601
    handler = callback_handler(db, service, fake)
    handler.handle_callback(
        callback(gate.id, human_gate_callback_data("blocked", gate.id))
    )
    assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "HUMAN_TEST"
    assert db.execute("SELECT state FROM projects").fetchone()[0] == "WAITING_HUMAN"
    assert db.execute("SELECT count(*) FROM human_test_results").fetchone()[0] == 0
    reason = "The required hardware sensor is unavailable until Monday."
    assert handler.handle_feedback_reply(
        TelegramInboundMessage(920, 602, 300, 42, reason, NOW, 7, 601)
    )
    assert tuple(
        db.execute("SELECT outcome,evidence_text FROM human_test_results").fetchone()
    ) == ("BLOCKED", reason)
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    assert db.execute("SELECT state FROM projects").fetchone()[0] == "BLOCKED"
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_stale_sha_between_fail_button_and_feedback_is_rejected(tmp_path: Path) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="stale-feedback",
        occurred_at=NOW,
    )
    bind_notification(db, gate.id)
    fake = FakeTelegram()
    fake.next_message_id = 701
    handler = callback_handler(db, service, fake)
    handler.handle_callback(
        callback(gate.id, human_gate_callback_data("fail", gate.id))
    )
    db.execute(
        "UPDATE pull_requests SET head_sha=?,updated_at=? WHERE id=?",
        (SHA_B, NOW.isoformat(), pr_id),
    )
    assert handler.handle_feedback_reply(
        TelegramInboundMessage(930, 702, 300, 42, "Observed failure", NOW, 7, 701)
    )
    assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    assert SQLiteM25TelegramFeedbackRepository(db).active(gate.id) is not None
    assert db.execute("SELECT count(*) FROM human_test_results").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind", [HumanDecisionKind.PRODUCT, HumanDecisionKind.TECHNICAL]
)
def test_decision_inline_options_render_and_resolve_from_persisted_index(
    tmp_path: Path, kind: HumanDecisionKind
) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED
    )
    assert review.human_gate is not None
    review = replace(review, human_gate=replace(review.human_gate, decision_kind=kind))
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="decision-button",
        occurred_at=NOW,
    )
    fake = FakeTelegram()
    notifier = TelegramGateNotifier(
        cast(TelegramClient, fake),
        300,
        7,
        notifications=SQLiteTelegramGateNotificationRepository(db),
        clock=lambda: NOW,
    )
    notifier.send(f"Decision\nGate: {gate.id}")
    keyboard = fake.sent[0]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    assert [row[0]["text"] for row in keyboard] == ["A", "B"]
    data = keyboard[1][0]["callback_data"]
    assert len(data.encode()) <= 64
    # Authority comes from persisted option index 1; the displayed label is irrelevant.
    keyboard[1][0]["text"] = "tampered label"
    callback_handler(db, service, fake).handle_callback(callback(gate.id, data))
    response = db.execute(
        "SELECT selected_option FROM human_gate_responses WHERE gate_id=?",
        (str(gate.id),),
    ).fetchone()
    assert response[0] == "B"


def test_inline_callback_rejects_foreign_unauthorised_duplicate_and_stale(
    tmp_path: Path,
) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="negative-buttons",
        occurred_at=NOW,
    )
    bind_notification(db, gate.id)
    data = human_gate_callback_data("pass", gate.id)
    fake = FakeTelegram()
    handler = callback_handler(db, service, fake)
    for invalid in (
        callback(gate.id, data, chat=999),
        callback(gate.id, data, message=999),
    ):
        assert handler.handle_callback(invalid)
        assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    with pytest.raises(PermissionError):
        handler.handle_callback(callback(gate.id, data, user=99))
    db.execute(
        "UPDATE pull_requests SET head_sha=?,updated_at=? WHERE id=?",
        (SHA_B, NOW.isoformat(), pr_id),
    )
    assert handler.handle_callback(callback(gate.id, data))
    assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    db.execute(
        "UPDATE pull_requests SET head_sha=?,updated_at=? WHERE id=?",
        (SHA_A, NOW.isoformat(), pr_id),
    )
    assert handler.handle_callback(callback(gate.id, data))
    assert service.gate_repository.get(gate.id).state is GateState.RESOLVED
    assert handler.handle_callback(callback(gate.id, data))
    assert db.execute("SELECT count(*) FROM human_gate_responses").fetchone()[0] == 1


def test_typed_gate_command_remains_available_after_inline_controls(
    tmp_path: Path,
) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="typed-fallback",
        occurred_at=NOW,
    )
    config = load_config(
        {"telegram": {"enabled": True, "authorised_user_ids": [42]}},
        environ={},
        secrets=SecretInputs(telegram_bot_token=SecretValue("synthetic-token")),
    )
    result = route_authorized_message(
        TelegramInboundMessage(99, 100, 300, 42, f"gate {gate.id} PASS", NOW),
        build_host_router(config, db),
    )
    assert "resolved as PASS" in result.text


@pytest.mark.parametrize("outcome", ["FAIL", "BLOCKED"])
def test_typed_fallback_rejects_evidenceless_negative_outcome(
    tmp_path: Path, outcome: str
) -> None:
    db, service, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="typed-negative",
        occurred_at=NOW,
    )
    config = load_config(
        {"telegram": {"enabled": True, "authorised_user_ids": [42]}},
        environ={},
        secrets=SecretInputs(telegram_bot_token=SecretValue("synthetic-token")),
    )
    result = route_authorized_message(
        TelegramInboundMessage(99, 100, 300, 42, f"gate {gate.id} {outcome}", NOW),
        build_host_router(config, db),
    )
    assert "require feedback" in result.text
    assert service.gate_repository.get(gate.id).state is GateState.NOTIFIED
    assert db.execute("SELECT count(*) FROM human_test_results").fetchone()[0] == 0


def test_migration_021_to_022_preserves_gate_response_and_review(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade.db"
    with open_database(path) as db:
        apply_migrations(db, MIGRATIONS[:21])
        assert current_schema_version(db) == 21
        ids = {
            name: str(uuid4())
            for name in (
                "project",
                "milestone",
                "repo",
                "pr",
                "request",
                "response",
                "review",
                "finding",
                "gate",
                "gate_response",
            )
        }
        stamp = NOW.isoformat()
        db.execute(
            "INSERT INTO projects (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES (?,?,'BUILDING',?,?,?,?, 'public')",
            (ids["project"], "history", stamp, stamp, stamp, "history"),
        )
        db.execute(
            "INSERT INTO milestones (id,project_id,sequence_number,code,title,state,created_at,updated_at,definition_json,automated_acceptance_json) VALUES (?,?,25,'M25','History','ARCHITECT_REVIEW',?,?,'{}','[]')",
            (ids["milestone"], ids["project"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO github_repositories VALUES (?,?, 'github','owner','repo','owner/repo',77,'public','main','VERIFIED',?,?,?)",
            (ids["repo"], ids["project"], stamp, stamp, stamp),
        )
        db.execute(
            "INSERT INTO pull_requests (id,project_id,milestone_id,github_repository_id,external_pr_number,state,head_branch,base_branch,head_sha,web_url,title,created_at,updated_at,last_reconciled_at) VALUES (?,?,?,?,25,'OPEN','branch','main',?,?,?,?,?,?)",
            (
                ids["pr"],
                ids["project"],
                ids["milestone"],
                ids["repo"],
                SHA_A,
                "https://example/pr/25",
                "title",
                stamp,
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO architect_requests (id,project_id,milestone_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status) VALUES (?,?,?,'REVIEW','fake','model','high','1.0','{}','history',?,?,'SUCCEEDED')",
            (ids["request"], ids["project"], ids["milestone"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO architect_responses (id,architect_request_id,response_type,response_schema_version,normalised_payload_json,status,created_at,validation_status,provider,model) VALUES (?,?,'REVIEW','1.0','{}','ACCEPTED',?,'VALID','fake','model')",
            (ids["response"], ids["request"], stamp),
        )
        db.execute(
            "INSERT INTO architect_reviews VALUES (?,?,?,?,?,?,'CHANGES_REQUIRED','history review',?,NULL)",
            (
                ids["review"],
                ids["project"],
                ids["milestone"],
                ids["request"],
                ids["pr"],
                SHA_A,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO architect_review_findings VALUES (?,?, 'finding-code','major','M25','description','action','OPEN',NULL,?)",
            (ids["finding"], ids["review"], stamp),
        )
        db.execute(
            "INSERT INTO human_gates (id,project_id,milestone_id,gate_type,state,title,prompt,expected_response_type,options_json,resume_milestone_state,created_at,notified_at,responded_at,resolved_at,created_by,correlation_id) VALUES (?,?,?,'TECHNICAL_DECISION','RESOLVED','Decision','Choose','OPTION','[\"A\",\"B\"]','ARCHITECT_REVIEW',?,?,?,?, 'SYSTEM','history')",
            (ids["gate"], ids["project"], ids["milestone"], stamp, stamp, stamp, stamp),
        )
        db.execute(
            "INSERT INTO human_gate_responses VALUES (?,?, 'telegram-message','A','feedback','A','[]','42',?,1,'valid')",
            (ids["gate_response"], ids["gate"], stamp),
        )
        apply_migrations(db, MIGRATIONS[:22])
        assert current_schema_version(db) == 22
        assert tuple(
            db.execute(
                "SELECT id,prompt,state FROM human_gates WHERE id=?", (ids["gate"],)
            ).fetchone()
        ) == (ids["gate"], "Choose", "RESOLVED")
        assert tuple(
            db.execute(
                "SELECT id,response_code,response_text FROM human_gate_responses WHERE id=?",
                (ids["gate_response"],),
            ).fetchone()
        ) == (ids["gate_response"], "A", "feedback")
        assert tuple(
            db.execute(
                "SELECT id,verdict,summary FROM architect_reviews WHERE id=?",
                (ids["review"],),
            ).fetchone()
        ) == (ids["review"], "CHANGES_REQUIRED", "history review")
        assert (
            db.execute(
                "SELECT finding_code FROM architect_review_findings WHERE id=?",
                (ids["finding"],),
            ).fetchone()[0]
            == "finding-code"
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_022_to_023_preserves_m25_data_and_adds_feedback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-22.db"
    with open_database(path) as db:
        apply_migrations(db, MIGRATIONS[:22])
        assert current_schema_version(db) == 22
        ids = {
            name: str(uuid4())
            for name in (
                "project",
                "milestone",
                "repo",
                "pr",
                "ci",
                "request",
                "response",
                "review",
                "gate",
                "gate_response",
                "test_result",
            )
        }
        stamp = NOW.isoformat()
        db.execute(
            "INSERT INTO projects (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES (?,?,'WAITING_HUMAN',?,?,?,?, 'public')",
            (ids["project"], "schema-22", stamp, stamp, stamp, "schema-22"),
        )
        db.execute(
            "INSERT INTO milestones (id,project_id,sequence_number,code,title,state,created_at,updated_at,definition_json,automated_acceptance_json) VALUES (?,?,25,'M25','Human test','HUMAN_TEST',?,?,'{}','[]')",
            (ids["milestone"], ids["project"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO github_repositories VALUES (?,?, 'github','owner','repo','owner/repo',77,'public','main','VERIFIED',?,?,?)",
            (ids["repo"], ids["project"], stamp, stamp, stamp),
        )
        db.execute(
            "INSERT INTO pull_requests (id,project_id,milestone_id,github_repository_id,external_pr_number,state,head_branch,base_branch,head_sha,web_url,title,created_at,updated_at,last_reconciled_at) VALUES (?,?,?,?,25,'OPEN','branch','main',?,?,?,?,?,?)",
            (
                ids["pr"],
                ids["project"],
                ids["milestone"],
                ids["repo"],
                SHA_A,
                "https://example/pr/25",
                "M25",
                stamp,
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO ci_runs (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,overall_status,started_at,completed_at,last_checked_at,summary_json,retry_count) VALUES (?,?,?,?,?,1,'PASSED',?,?,?,'{}',0)",
            (
                ids["ci"],
                ids["project"],
                ids["milestone"],
                ids["pr"],
                SHA_A,
                stamp,
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO architect_requests (id,project_id,milestone_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status) VALUES (?,?,?,'REVIEW','fake','model','high','1.0','{}','schema-22',?,?,'SUCCEEDED')",
            (ids["request"], ids["project"], ids["milestone"], stamp, stamp),
        )
        db.execute(
            "INSERT INTO architect_responses (id,architect_request_id,response_type,response_schema_version,normalised_payload_json,status,created_at,validation_status,provider,model) VALUES (?,?,'REVIEW','1.0','{}','ACCEPTED',?,'VALID','fake','model')",
            (ids["response"], ids["request"], stamp),
        )
        db.execute(
            "INSERT INTO architect_reviews VALUES (?,?,?,?,?,?,'HUMAN_TEST_REQUIRED','test required',?,NULL)",
            (
                ids["review"],
                ids["project"],
                ids["milestone"],
                ids["request"],
                ids["pr"],
                SHA_A,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO human_gates (id,project_id,milestone_id,gate_type,state,title,prompt,expected_response_type,options_json,resume_milestone_state,created_at,notified_at,responded_at,resolved_at,created_by,correlation_id,architect_review_id,causation_id) VALUES (?,?,?,'HUMAN_TEST','RESOLVED','Test','Exercise build','HUMAN_TEST','[\"PASS\",\"FAIL\",\"BLOCKED\"]','ARCHITECT_REVIEW',?,?,?,?, 'ARCHITECT','schema-22',?,'review-event')",
            (
                ids["gate"],
                ids["project"],
                ids["milestone"],
                stamp,
                stamp,
                stamp,
                stamp,
                ids["review"],
            ),
        )
        db.execute(
            "INSERT INTO human_gate_responses VALUES (?,?, 'telegram-message','PASS','passed','PASS','[]','42',?,1,'valid')",
            (ids["gate_response"], ids["gate"], stamp),
        )
        db.execute(
            "INSERT INTO human_test_bindings VALUES (?,?,?,?,?,25,?,?, 'artifact://schema-22','Exercise build',?)",
            (
                ids["gate"],
                ids["project"],
                ids["milestone"],
                ids["review"],
                ids["pr"],
                SHA_A,
                ids["ci"],
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO human_test_results VALUES (?,?,?,'PASS','passed',?,?,?)",
            (
                ids["test_result"],
                ids["gate"],
                ids["gate_response"],
                SHA_A,
                ids["ci"],
                stamp,
            ),
        )

        apply_migrations(db)

        assert current_schema_version(db) == 23
        assert tuple(
            db.execute(
                "SELECT outcome,evidence_text,tested_head_sha,ci_run_id FROM human_test_results WHERE id=?",
                (ids["test_result"],),
            ).fetchone()
        ) == ("PASS", "passed", SHA_A, ids["ci"])
        objects = {
            (row[0], row[1])
            for row in db.execute(
                "SELECT name,type FROM sqlite_master WHERE name IN ("
                "'m25_telegram_feedback_interactions','one_active_m25_feedback_per_gate',"
                "'m25_feedback_identity_immutable','m25_feedback_no_delete')"
            )
        }
        assert objects == {
            ("m25_telegram_feedback_interactions", "table"),
            ("one_active_m25_feedback_per_gate", "index"),
            ("m25_feedback_identity_immutable", "trigger"),
            ("m25_feedback_no_delete", "trigger"),
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_clean_database_migrates_through_023(tmp_path: Path) -> None:
    with open_database(tmp_path / "clean.db") as db:
        apply_migrations(db)
        assert current_schema_version(db) == 23
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
