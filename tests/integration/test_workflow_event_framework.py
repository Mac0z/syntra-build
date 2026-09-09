"""M11 durable event persistence, dispatch, deduplication, and atomicity."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from syntra_build.application.events import (
    EventProcessingResult,
    WorkflowEventDispatcher,
)
from syntra_build.domain import (
    DuplicateEvent,
    EventProcessingStatus,
    GateId,
    InsertedEvent,
    JobId,
    Milestone,
    MilestoneId,
    MilestoneState,
    MilestoneTransitionRequest,
    Project,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    WorkflowEvent,
    WorkflowEventId,
    WorkflowEventSource,
)
from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    EventAlreadyProcessedError,
    EventClaimConflictError,
    EventParentMismatchError,
    InvalidEventCausationError,
    PersistenceError,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    SQLiteWorkflowEventRepository,
    apply_migrations,
    current_schema_version,
    open_database,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
P1 = ProjectId.from_string("00000000-0000-0000-0000-000000000001")
P2 = ProjectId.from_string("00000000-0000-0000-0000-000000000002")
M1 = MilestoneId.from_string("00000000-0000-0000-0000-000000000011")
M2 = MilestoneId.from_string("00000000-0000-0000-0000-000000000012")
J1 = JobId.from_string("00000000-0000-0000-0000-000000000021")
G1 = GateId.from_string("00000000-0000-0000-0000-000000000031")


def setup(
    path: Path,
) -> tuple[
    sqlite3.Connection,
    SQLiteWorkflowEventRepository,
    SQLiteProjectRepository,
]:
    db = open_database(path)
    apply_migrations(db)
    projects = SQLiteProjectRepository(db, lambda: "transition-1")
    projects.add(Project(P1, "one", ProjectState.NEW, NOW, NOW))
    projects.add(Project(P2, "two", ProjectState.NEW, NOW, NOW))
    milestones = SQLiteMilestoneRepository(db, lambda: "milestone-transition")
    milestones.add(Milestone(M1, P1, 1, "M1", "one", MilestoneState.PENDING, NOW, NOW))
    milestones.add(Milestone(M2, P2, 1, "M1", "two", MilestoneState.PENDING, NOW, NOW))
    return db, SQLiteWorkflowEventRepository(db), projects


def make_event(
    *,
    event_id: WorkflowEventId | None = None,
    project_id: ProjectId = P1,
    event_type: str = "TEST",
    source: WorkflowEventSource = WorkflowEventSource.INTERNAL,
    key: str | None = None,
    cause: WorkflowEventId | None = None,
    milestone_id: MilestoneId | None = None,
    job_id: JobId | None = None,
    gate_id: GateId | None = None,
) -> WorkflowEvent:
    return WorkflowEvent(
        event_id or WorkflowEventId.generate(),
        project_id,
        event_type,
        NOW,
        "corr-11",
        milestone_id=milestone_id,
        job_id=job_id,
        gate_id=gate_id,
        payload={"nested": [1, {"ok": True}]},
        source=source,
        received_at=NOW + timedelta(seconds=1),
        causation_event_id=cause,
        external_deduplication_key=key,
    )


class Handler:
    def __init__(
        self,
        outcomes: list[EventProcessingResult],
        effect: Callable[[WorkflowEvent], None] | None = None,
    ):
        self.outcomes = outcomes
        self.effect = effect
        self.calls = 0

    def handle(self, event: WorkflowEvent) -> EventProcessingResult:
        self.calls += 1
        if self.effect:
            self.effect(event)
        return self.outcomes.pop(0)


def dispatcher(
    repo: SQLiteWorkflowEventRepository,
    handler: Handler | None,
    event_type: str = "TEST",
) -> WorkflowEventDispatcher:
    handlers = {} if handler is None else {event_type: handler}
    return WorkflowEventDispatcher(
        repo,
        handlers,
        clock=lambda: NOW + timedelta(seconds=2),
        claim_token_factory=lambda: "claim-1",
    )


def test_insert_round_trip_queries_and_external_deduplication(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "events.db")
    stamp = NOW.isoformat()
    db.execute(
        """INSERT INTO jobs
        (id,project_id,milestone_id,job_type,state,priority,correlation_id,
         attempt_number,max_attempts,worker_class,payload_json,result_json,
         created_at,updated_at) VALUES (?,?,?,'TEST','QUEUED',0,'corr-11',0,1,
         'INTERNAL','{}','{}',?,?)""",
        (str(J1), str(P1), str(M1), stamp, stamp),
    )
    db.execute(
        """INSERT INTO human_gates
        (id,project_id,milestone_id,gate_type,state,title,prompt,
         expected_response_type,options_json,created_at,created_by,correlation_id)
         VALUES (?,?,?,'HUMAN_TEST','PENDING','test','test','HUMAN_TEST','[]',
         ?,'SYSTEM','corr-11')""",
        (str(G1), str(P1), str(M1), stamp),
    )
    first = make_event(
        source=WorkflowEventSource.TELEGRAM,
        key="telegram:update:1",
        milestone_id=M1,
        job_id=J1,
        gate_id=G1,
    )
    inserted = repo.add(first)
    duplicate = repo.add(
        make_event(source=WorkflowEventSource.TELEGRAM, key="telegram:update:1")
    )
    second = repo.add(
        make_event(source=WorkflowEventSource.TELEGRAM, key="telegram:update:2")
    )
    assert isinstance(inserted, InsertedEvent)
    assert isinstance(duplicate, DuplicateEvent)
    assert isinstance(second, InsertedEvent)
    assert duplicate.record.event.id == first.id
    stored = repo.get(first.id)
    assert stored.event.payload == {"nested": (1, {"ok": True})}
    assert stored.event.milestone_id == M1
    assert stored.event.job_id == J1
    assert stored.event.gate_id == G1
    assert stored.processing_status is EventProcessingStatus.PENDING
    assert stored.processing_attempt_count == 0
    assert repo.find_by_external_deduplication_key("telegram:update:1") == stored
    expected = sorted((first.id, second.record.event.id), key=str)
    assert [item.event.id for item in repo.list_pending()] == expected
    assert len(repo.for_project(P1)) == 2
    assert db.execute("SELECT count(*) FROM workflow_events").fetchone()[0] == 2
    db.close()


def test_envelope_is_database_immutable_and_append_only(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "immutable.db")
    identity = make_event().id
    repo.add(make_event(event_id=identity))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "UPDATE workflow_events SET payload_json='{}' WHERE id=?", (str(identity),)
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM workflow_events WHERE id=?", (str(identity),))
    db.close()


def test_parent_and_causation_integrity(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "identity.db")
    root = make_event()
    repo.add(root)
    child = make_event(cause=root.id)
    assert isinstance(repo.add(child), InsertedEvent)
    with pytest.raises(InvalidEventCausationError):
        repo.add(make_event(cause=WorkflowEventId.generate()))
    with pytest.raises(InvalidEventCausationError):
        repo.add(make_event(project_id=P2, cause=root.id))
    with pytest.raises(EventParentMismatchError):
        repo.add(make_event(project_id=P1, milestone_id=M2))
    db.close()


def test_dispatch_success_rejection_unknown_and_terminal_protection(
    tmp_path: Path,
) -> None:
    db, repo, _ = setup(tmp_path / "dispatch.db")
    success = make_event()
    repo.add(success)
    handled = Handler([EventProcessingResult.processed()])
    record = dispatcher(repo, handled).dispatch(success.id)
    assert record.processing_status is EventProcessingStatus.PROCESSED
    assert record.processing_attempt_count == 1 and record.processed_at is not None
    with pytest.raises(EventAlreadyProcessedError):
        dispatcher(repo, handled).dispatch(success.id)
    assert handled.calls == 1

    rejected = make_event(event_type="REJECT")
    repo.add(rejected)
    rejecting = Handler([EventProcessingResult.rejected("stale revision")])
    record = dispatcher(repo, rejecting, "REJECT").dispatch(rejected.id)
    assert record.processing_status is EventProcessingStatus.REJECTED
    assert record.last_processing_error == "stale revision"
    unknown = make_event(event_type="UNKNOWN")
    repo.add(unknown)
    assert (
        dispatcher(repo, None).dispatch(unknown.id).processing_status
        is EventProcessingStatus.REJECTED
    )
    db.close()


def test_failed_event_is_visible_and_explicitly_retryable(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "retry.db")
    item = make_event()
    repo.add(item)
    handler = Handler(
        [EventProcessingResult.failed("temporary"), EventProcessingResult.processed()]
    )
    first = dispatcher(repo, handler).dispatch(item.id)
    assert first.processing_status is EventProcessingStatus.FAILED
    assert first.processing_attempt_count == 1
    assert first.last_processing_error == "temporary"
    assert repo.list_failed() == (first,)
    retried = dispatcher(repo, handler).dispatch(item.id)
    assert retried.processing_status is EventProcessingStatus.PROCESSED
    assert retried.processing_attempt_count == 2
    assert retried.last_processing_error == "temporary"
    db.close()


def test_state_effect_completion_and_history_are_one_atomic_unit(
    tmp_path: Path,
) -> None:
    db, repo, projects = setup(tmp_path / "atomic.db")
    item = make_event(source=WorkflowEventSource.TELEGRAM, key="telegram:update:9")
    repo.add(item)

    def transition(event: WorkflowEvent) -> None:
        projects.apply_transition(
            ProjectTransitionRequest(
                event.project_id,
                ProjectState.NEW,
                ProjectState.DESIGNING,
                "event requested design",
                "SYSTEM",
                None,
                event.correlation_id,
                NOW + timedelta(seconds=2),
                str(event.id),
            )
        )

    handler = Handler([EventProcessingResult.processed()], transition)
    assert (
        dispatcher(repo, handler).dispatch(item.id).processing_status
        is EventProcessingStatus.PROCESSED
    )
    assert projects.get(P1).state is ProjectState.DESIGNING
    assert projects.transitions(P1)[0].trigger_event_id == str(item.id)
    duplicate = repo.add(
        make_event(source=WorkflowEventSource.TELEGRAM, key="telegram:update:9")
    )
    assert isinstance(duplicate, DuplicateEvent)
    with pytest.raises(EventAlreadyProcessedError):
        dispatcher(repo, handler).dispatch(duplicate.record.event.id)
    assert handler.calls == 1 and len(projects.transitions(P1)) == 1
    db.close()


def test_handler_exception_rolls_back_state_effect_before_recording_failure(
    tmp_path: Path,
) -> None:
    db, repo, projects = setup(tmp_path / "rollback.db")
    item = make_event()
    repo.add(item)

    def transition_then_fail(event: WorkflowEvent) -> None:
        projects.apply_transition(
            ProjectTransitionRequest(
                event.project_id,
                ProjectState.NEW,
                ProjectState.DESIGNING,
                "must roll back",
                "SYSTEM",
                None,
                event.correlation_id,
                NOW + timedelta(seconds=2),
                str(event.id),
            )
        )
        raise RuntimeError("forced completion failure")

    failed = dispatcher(
        repo, Handler([EventProcessingResult.processed()], transition_then_fail)
    ).dispatch(item.id)
    assert failed.processing_status is EventProcessingStatus.FAILED
    assert "forced completion failure" in (failed.last_processing_error or "")
    assert projects.get(P1).state is ProjectState.NEW
    assert projects.transitions(P1) == ()
    db.close()


def test_event_completion_write_failure_rolls_back_trusted_effect(
    tmp_path: Path,
) -> None:
    db, repo, projects = setup(tmp_path / "completion-rollback.db")
    item = make_event()
    repo.add(item)
    db.execute(
        """CREATE TRIGGER force_event_completion_failure
        BEFORE UPDATE OF processing_status ON workflow_events
        BEGIN SELECT RAISE(ABORT,'forced event completion failure'); END"""
    )

    def transition(event: WorkflowEvent) -> None:
        projects.apply_transition(
            ProjectTransitionRequest(
                event.project_id,
                ProjectState.NEW,
                ProjectState.DESIGNING,
                "must roll back",
                "SYSTEM",
                None,
                event.correlation_id,
                NOW + timedelta(seconds=2),
                str(event.id),
            )
        )

    with pytest.raises(PersistenceError):
        dispatcher(
            repo, Handler([EventProcessingResult.processed()], transition)
        ).dispatch(item.id)
    assert projects.get(P1).state is ProjectState.NEW
    assert projects.transitions(P1) == ()
    assert repo.get(item.id).processing_status is EventProcessingStatus.PENDING
    db.close()


def test_milestone_identity_and_trigger_link_survive_dispatch(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "milestone-effect.db")
    milestones = SQLiteMilestoneRepository(db, lambda: "milestone-event-transition")
    item = make_event(event_type="MILESTONE_TEST", milestone_id=M1)
    repo.add(item)

    def transition(event: WorkflowEvent) -> None:
        assert event.milestone_id is not None
        milestones.apply_transition(
            MilestoneTransitionRequest(
                event.milestone_id,
                event.project_id,
                MilestoneState.PENDING,
                MilestoneState.READY,
                "event requested readiness",
                "SYSTEM",
                None,
                event.correlation_id,
                NOW + timedelta(seconds=2),
                str(event.id),
            )
        )

    handler = Handler([EventProcessingResult.processed()], transition)
    record = dispatcher(repo, handler, "MILESTONE_TEST").dispatch(item.id)
    assert record.event.milestone_id == M1
    assert milestones.get(M1, P1).state is MilestoneState.READY
    assert milestones.transitions(M1, P1)[0].trigger_event_id == str(item.id)
    db.close()


def test_stale_claim_is_classifiable_and_does_not_call_handler(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "claim.db")
    item = make_event()
    repo.add(item)
    db.execute(
        "UPDATE workflow_events SET processing_claim_token='other' WHERE id=?",
        (str(item.id),),
    )
    handler = Handler([EventProcessingResult.processed()])
    with pytest.raises(EventClaimConflictError):
        dispatcher(repo, handler).dispatch(item.id)
    assert handler.calls == 0
    db.close()


def test_v5_upgrade_preserves_prior_tables_and_adds_only_m11_schema(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "upgrade.db") as db:
        apply_migrations(db, MIGRATIONS[:5])
        db.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?)",
            (
                str(P1),
                "existing",
                "NEW",
                None,
                None,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        apply_migrations(db)
        assert current_schema_version(db) == len(MIGRATIONS)
        assert (
            db.execute("SELECT name FROM projects WHERE id=?", (str(P1),)).fetchone()[0]
            == "existing"
        )
        assert db.execute("SELECT count(*) FROM workflow_events").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
