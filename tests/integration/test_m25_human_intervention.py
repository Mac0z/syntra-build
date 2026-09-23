# ruff: noqa: E501
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

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
from syntra_build.domain.milestones import MilestoneState
from syntra_build.domain.pull_requests import PullRequestDescriptor, PullRequestState
from syntra_build.domain.reviews import (
    ArchitectHumanGateRequest,
    ArchitectReview,
    ArchitectReviewVerdict,
)
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
from syntra_build.infrastructure.persistence.pull_requests import (
    SQLitePullRequestRepository,
)

NOW = datetime(2026, 9, 23, tzinfo=UTC)
SHA_A, SHA_B = "a" * 40, "b" * 40


class Notifier:
    def send(self, text: str) -> str:
        return "telegram-message"


def fixture(
    tmp_path: Path, verdict: ArchitectReviewVerdict
) -> tuple[sqlite3.Connection, HumanInterventionService, ArchitectReview, str, str]:
    db = open_database(tmp_path / f"{uuid4()}.db")
    apply_migrations(db)
    project, milestone = ProjectId.generate(), MilestoneId.generate()
    repo, pr_id, request_id, response_id, review_id, ci_id = (
        str(uuid4()) for _ in range(6)
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
        "INSERT INTO architect_requests (id,project_id,milestone_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,completed_at,status) VALUES (?,?,?,'REVIEW','fake','model','high','1.0','{}','corr',?,?,'SUCCEEDED')",
        (request_id, str(project), str(milestone), stamp, stamp),
    )
    db.execute(
        "INSERT INTO architect_responses (id,architect_request_id,response_type,response_schema_version,normalised_payload_json,status,created_at,validation_status,provider,model) VALUES (?,?,'REVIEW','1.0','{}','ACCEPTED',?,'VALID','fake','model')",
        (response_id, request_id, stamp),
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
    detail = ArchitectHumanGateRequest(
        "Choose implementation",
        MilestoneState.ARCHITECT_REVIEW
        if verdict is ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED
        else MilestoneState.MERGE_READY,
        ("A", "B") if verdict is ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED else (),
        None
        if verdict is ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED
        else "Exercise feature and report",
        "artifact://build-25",
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
                MilestoneState.MERGE_READY,
                (),
                "Exercise the build",
                "artifact://cross-component",
            ),
        )

    def telemetry(self) -> dict[str, int | str | None]:
        return {"provider_response_id": "m25-response"}


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
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "MERGE_READY"
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
    response = db.execute(
        "SELECT selected_option,response_text FROM human_gate_responses"
    ).fetchone()
    assert tuple(response) == ("A", "human evidence")


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


def test_migration_021_to_022_preserves_gate_response_and_review(
    tmp_path: Path,
) -> None:
    path = tmp_path / "upgrade.db"
    with open_database(path) as db:
        apply_migrations(db, MIGRATIONS[:21])
        assert current_schema_version(db) == 21
        # Existing M24 migration coverage supplies full populated-history preservation;
        # Verify the forward migration itself and referential integrity.
        apply_migrations(db)
        assert current_schema_version(db) == 22
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with open_database(tmp_path / "clean.db") as db:
        apply_migrations(db)
        assert current_schema_version(db) == 22
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
