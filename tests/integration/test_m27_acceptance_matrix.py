# mypy: disable-error-code="no-untyped-call,no-untyped-def"
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from test_m22_pull_request_lifecycle import CHANGE_SET, service
from test_m22_pull_request_lifecycle import NOW as PR_NOW
from test_m25_human_intervention import NOW as HUMAN_NOW
from test_m25_human_intervention import Notifier, fixture

from syntra_build.application.ci_handoff import PullRequestCIHandoff
from syntra_build.application.ci_monitor import CIMonitor
from syntra_build.application.recovery import RecoveryCoordinator, RecoveryServices
from syntra_build.application.scheduler import Scheduler, WorkerCapacity
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIObservation,
)
from syntra_build.domain.gates import GateState
from syntra_build.domain.reviews import ArchitectReviewVerdict
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence import apply_migrations, open_database
from syntra_build.infrastructure.persistence.jobs import SQLiteJobRepository


class Actions:
    def __init__(
        self,
        status: CICheckStatus,
        conclusion: CICheckConclusion | None = None,
    ) -> None:
        self.status = status
        self.conclusion = conclusion
        self.observations = 0
        self.reruns = 0

    def observe(self, repository: str, number: int, sha: str) -> CIObservation:
        self.observations += 1
        return CIObservation(
            (CICheck("required", "job", self.status, self.conclusion),), ("run-1",)
        )

    def rerun(self, repository: str, workflow_run_id: str) -> None:
        self.reruns += 1


def scheduler(db) -> Scheduler:
    return Scheduler(
        SQLiteJobRepository(db, lambda: str(uuid4())),
        WorkerCapacity(SchedulerConfig().worker_class_limits()),
        {},
    )


def ci_fixture(
    tmp_path: Path,
    status: CICheckStatus,
    conclusion: CICheckConclusion | None = None,
):
    db = open_database(tmp_path / f"ci-{status}.db")
    apply_migrations(db)
    lifecycle, _workspace, github, project, milestone = service(db)
    record = PullRequestCIHandoff(db, lifecycle).establish(
        project, milestone, CHANGE_SET, "ci-setup", now=PR_NOW
    )
    actions = Actions(status, conclusion)
    monitor = CIMonitor(
        db, github, actions, clock=lambda: PR_NOW + timedelta(seconds=2)
    )
    return db, project, milestone, record, actions, monitor


def test_ci_running_recovery_restores_exact_head_wait_without_duplicate_run(
    tmp_path: Path,
) -> None:
    db, project, milestone, record, actions, monitor = ci_fixture(
        tmp_path, CICheckStatus.RUNNING
    )
    recovery_scheduler = scheduler(db)
    coordinator = RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(ci=monitor),
        clock=lambda: PR_NOW + timedelta(seconds=2),
    )
    coordinator.recover(correlation_id="ci-running")
    coordinator.recover(correlation_id="ci-running-again")
    rows = db.execute(
        """SELECT head_sha,attempt_number,overall_status FROM ci_runs
           WHERE pull_request_id=?""",
        (record.id,),
    ).fetchall()
    assert [(row["head_sha"], row["attempt_number"]) for row in rows] == [
        (record.head_sha, 1)
    ]
    assert rows[0]["overall_status"] == "RUNNING"
    assert actions.reruns == 0 and actions.observations == 2
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "CI_RUNNING"
    recovery_scheduler.close()
    db.close()


def test_ci_running_recovery_exact_head_pass_advances_normally(tmp_path: Path) -> None:
    db, _project, _milestone, record, actions, monitor = ci_fixture(
        tmp_path, CICheckStatus.COMPLETED, CICheckConclusion.PASSED
    )
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(ci=monitor),
        clock=lambda: PR_NOW + timedelta(seconds=2),
    ).recover(correlation_id="ci-pass")
    run = db.execute(
        "SELECT * FROM ci_runs WHERE pull_request_id=?", (record.id,)
    ).fetchone()
    assert run["overall_status"] == "PASSED" and run["head_sha"] == record.head_sha
    assert (
        db.execute("SELECT state FROM milestones").fetchone()[0] == "ARCHITECT_REVIEW"
    )
    assert actions.reruns == 0
    recovery_scheduler.close()
    db.close()


def test_ci_running_recovery_rejects_stale_pass(tmp_path: Path) -> None:
    db, _project, _milestone, record, _actions, monitor = ci_fixture(
        tmp_path, CICheckStatus.COMPLETED, CICheckConclusion.PASSED
    )
    db.execute(
        """INSERT INTO ci_runs
           (id,project_id,milestone_id,pull_request_id,head_sha,attempt_number,
            overall_status,started_at,completed_at,last_checked_at,summary_json,retry_count)
           SELECT ?,project_id,milestone_id,pull_request_id,?,1,'PASSED',started_at,
            started_at,started_at,'{}',0 FROM ci_runs WHERE 0""",
        (str(uuid4()), "a" * 40),
    )
    # The coordinator passes the current persisted PR head to CIMonitor; a stale
    # provider PR observation therefore fails closed before policy can advance.
    original = monitor.github_prs.remote[0]
    monitor.github_prs.remote[0] = original.__class__(
        original.interface_version,
        original.project_id,
        original.milestone_id,
        original.repository_id,
        original.pull_request_number,
        original.state,
        original.head_branch,
        original.base_branch,
        "a" * 40,
        original.web_url,
    )
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(ci=monitor),
        clock=lambda: PR_NOW + timedelta(seconds=2),
    ).recover(correlation_id="ci-stale")
    assert db.execute("SELECT state FROM milestones").fetchone()[0] == "BLOCKED"
    assert (
        db.execute("SELECT head_sha FROM pull_requests").fetchone()[0]
        == record.head_sha
    )
    recovery_scheduler.close()
    db.close()


def test_human_test_notified_gate_is_restored_without_duplicate(tmp_path: Path) -> None:
    db, service_, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service_.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="cause",
        occurred_at=HUMAN_NOW,
    )
    notifier = cast(Notifier, service_.notifier)
    before = tuple(db.execute("SELECT * FROM human_test_bindings").fetchone())
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(human=service_),
        clock=lambda: HUMAN_NOW + timedelta(seconds=1),
    ).recover(correlation_id="human-notified")
    assert db.execute("SELECT count(*) FROM human_gates").fetchone()[0] == 1
    assert db.execute("SELECT id FROM human_gates").fetchone()[0] == str(gate.id)
    assert tuple(db.execute("SELECT * FROM human_test_bindings").fetchone()) == before
    assert db.execute("SELECT count(*) FROM human_gate_responses").fetchone()[0] == 0
    assert notifier.calls == 1
    recovery_scheduler.close()
    db.close()


def test_human_test_pending_gate_notifies_existing_gate(tmp_path: Path) -> None:
    db, initial, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    initial.notifier = None
    gate = initial.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="cause",
        occurred_at=HUMAN_NOW,
    )
    notifier = Notifier()
    from syntra_build.application.human_intervention import HumanInterventionService

    recovering = HumanInterventionService(
        db,
        authorised_responder_ids=frozenset({"42"}),
        notifier=notifier,
        clock=lambda: HUMAN_NOW + timedelta(seconds=1),
    )
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(human=recovering),
        clock=lambda: HUMAN_NOW + timedelta(seconds=1),
    ).recover(correlation_id="human-pending")
    row = db.execute("SELECT id,state FROM human_gates").fetchone()
    assert row["id"] == str(gate.id) and row["state"] == GateState.NOTIFIED.value
    assert notifier.calls == 1
    recovery_scheduler.close()
    db.close()


def test_human_test_resolved_pass_finishes_interrupted_handoff(tmp_path: Path) -> None:
    db, service_, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service_.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="cause",
        occurred_at=HUMAN_NOW,
    )
    response_id = str(uuid4())
    db.execute(
        """INSERT INTO human_gate_responses
           (id,gate_id,message_id,response_code,attachments_json,responded_by,
            responded_at,validated,validation_notes)
           VALUES (?,?,?,'PASS','[]','42',?,1,'valid')""",
        (response_id, str(gate.id), "message-pass", HUMAN_NOW.isoformat()),
    )
    db.execute(
        """UPDATE human_gates SET state='RESOLVED',responded_at=?,resolved_at=?
           WHERE id=?""",
        (HUMAN_NOW.isoformat(), HUMAN_NOW.isoformat(), str(gate.id)),
    )
    db.execute(
        """INSERT INTO human_test_results
           SELECT ?,b.gate_id,?,'PASS',NULL,b.tested_head_sha,b.ci_run_id,?
           FROM human_test_bindings b WHERE b.gate_id=?""",
        (str(uuid4()), response_id, HUMAN_NOW.isoformat(), str(gate.id)),
    )
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(human=service_),
        clock=lambda: HUMAN_NOW + timedelta(seconds=1),
    ).recover(correlation_id="resolved-pass")
    assert (
        db.execute("SELECT state FROM milestones").fetchone()[0] == "ARCHITECT_REVIEW"
    )
    assert db.execute("SELECT state FROM projects").fetchone()[0] == "BUILDING"
    assert db.execute("SELECT count(*) FROM human_gates").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM human_gate_responses").fetchone()[0] == 1
    recovery_scheduler.close()
    db.close()


def test_human_test_feedback_interaction_is_preserved(tmp_path: Path) -> None:
    db, service_, review, pr_id, ci_id = fixture(
        tmp_path, ArchitectReviewVerdict.HUMAN_TEST_REQUIRED
    )
    gate = service_.create_from_review(
        db.execute("SELECT id FROM architect_reviews").fetchone()[0],
        review,
        pr_id,
        ci_id,
        causation_id="cause",
        occurred_at=HUMAN_NOW,
    )
    interaction = str(uuid4())
    db.execute(
        """INSERT INTO m25_telegram_feedback_interactions
           (id,gate_id,outcome,chat_id,user_id,prompt_message_id,state,created_at)
           VALUES (?,?,'FAIL','300','42','prompt-1','WAITING_FEEDBACK',?)""",
        (interaction, str(gate.id), HUMAN_NOW.isoformat()),
    )
    before = tuple(
        db.execute("SELECT * FROM m25_telegram_feedback_interactions").fetchone()
    )
    recovery_scheduler = scheduler(db)
    RecoveryCoordinator(
        db,
        recovery_scheduler,
        services=RecoveryServices(human=service_),
        clock=lambda: HUMAN_NOW + timedelta(seconds=1),
    ).recover(correlation_id="feedback")
    assert (
        tuple(db.execute("SELECT * FROM m25_telegram_feedback_interactions").fetchone())
        == before
    )
    assert (
        db.execute(
            "SELECT count(*) FROM m25_telegram_feedback_interactions"
        ).fetchone()[0]
        == 1
    )
    recovery_scheduler.close()
    db.close()
