"""Tests for provider-independent M4 domain records."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.domain import (
    DomainValidationError,
    GateId,
    GateState,
    GateType,
    HumanGate,
    Job,
    JobId,
    JobState,
    Milestone,
    MilestoneId,
    MilestoneState,
    Project,
    ProjectId,
    ProjectState,
    WorkflowEvent,
    WorkflowEventId,
)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
UUID_TEXT = "12345678-1234-5678-1234-567812345678"


@pytest.mark.parametrize(
    "identifier_type", [ProjectId, MilestoneId, JobId, GateId, WorkflowEventId]
)
def test_identifier_generation_and_deterministic_parsing(
    identifier_type: type[ProjectId],
) -> None:
    generated = identifier_type.generate()
    assert UUID(str(generated)) == generated.value

    explicit = identifier_type.from_string(UUID_TEXT)
    assert str(explicit) == UUID_TEXT
    assert explicit == identifier_type.from_string(UUID_TEXT)
    assert hash(explicit) == hash(identifier_type.from_string(UUID_TEXT))


@pytest.mark.parametrize("invalid", ["", "   ", "not-a-uuid"])
def test_invalid_identifier_is_rejected(invalid: str) -> None:
    with pytest.raises(DomainValidationError):
        ProjectId.from_string(invalid)


def test_identifier_subtypes_do_not_compare_equal() -> None:
    project_identifier: object = ProjectId.from_string(UUID_TEXT)
    milestone_identifier: object = MilestoneId.from_string(UUID_TEXT)
    assert project_identifier != milestone_identifier


def test_state_enums_have_exact_approved_values() -> None:
    assert [state.value for state in ProjectState] == [
        "NEW",
        "DESIGNING",
        "DESIGN_APPROVAL",
        "PROVISIONING",
        "READY",
        "BUILDING",
        "WAITING_HUMAN",
        "PAUSED",
        "BLOCKED",
        "COMPLETING",
        "COMPLETE",
        "FAILED",
        "CANCELLED",
    ]
    assert [state.value for state in MilestoneState] == [
        "PENDING",
        "READY",
        "PREPARING_TASK",
        "PREPARING_WORKSPACE",
        "CODING",
        "VALIDATING_CHANGES",
        "COMMITTING",
        "PUSHING",
        "PR_CREATING",
        "CI_RUNNING",
        "CI_REWORK",
        "ARCHITECT_REVIEW",
        "REVIEW_REWORK",
        "HUMAN_DECISION",
        "HUMAN_TEST",
        "MERGE_READY",
        "MERGING",
        "MERGE_VERIFY",
        "COMPLETE",
        "BLOCKED",
        "FAILED",
        "CANCELLED",
    ]
    assert "ARCHITECT_APPROVED" not in MilestoneState.__members__
    assert [state.value for state in JobState] == [
        "QUEUED",
        "DISPATCHED",
        "RUNNING",
        "WAITING_EXTERNAL",
        "SUCCEEDED",
        "RETRY_WAIT",
        "FAILED",
        "CANCELLED",
        "ABANDONED",
    ]
    assert [state.value for state in GateState] == [
        "PENDING",
        "NOTIFIED",
        "RESPONDED",
        "VALIDATED",
        "RESOLVED",
        "EXPIRED",
        "CANCELLED",
    ]


def project_id() -> ProjectId:
    return ProjectId.from_string("00000000-0000-0000-0000-000000000001")


def milestone_id() -> MilestoneId:
    return MilestoneId.from_string("00000000-0000-0000-0000-000000000002")


def test_valid_project_is_immutable() -> None:
    project = Project(project_id(), "Syntra", ProjectState.NEW, NOW, LATER)
    assert project.name == "Syntra"
    with pytest.raises(FrozenInstanceError):
        project.name = "Changed"  # type: ignore[misc]


@pytest.mark.parametrize("name", ["", "  "])
def test_project_rejects_empty_name(name: str) -> None:
    with pytest.raises(DomainValidationError):
        Project(project_id(), name, ProjectState.NEW, NOW, NOW)


@pytest.mark.parametrize(
    ("created_at", "updated_at"),
    [(NOW.replace(tzinfo=None), NOW), (NOW, NOW.replace(tzinfo=None)), (LATER, NOW)],
)
def test_project_rejects_invalid_timestamps(
    created_at: datetime, updated_at: datetime
) -> None:
    with pytest.raises(DomainValidationError):
        Project(project_id(), "Syntra", ProjectState.NEW, created_at, updated_at)


def test_domain_timestamps_must_be_utc() -> None:
    non_utc = NOW.astimezone(timezone(timedelta(hours=1)))
    with pytest.raises(DomainValidationError, match="must be in UTC"):
        Project(project_id(), "Syntra", ProjectState.NEW, non_utc, non_utc)


def test_valid_milestone() -> None:
    milestone = Milestone(
        milestone_id(),
        project_id(),
        0,
        "M0",
        "Bootstrap",
        MilestoneState.PENDING,
        NOW,
        NOW,
    )
    assert milestone.sequence_number == 0
    assert milestone.project_id == project_id()


@pytest.mark.parametrize(("sequence", "code"), [(-1, "M4"), (4, "")])
def test_milestone_rejects_invalid_order_or_code(sequence: int, code: str) -> None:
    with pytest.raises(DomainValidationError):
        Milestone(
            milestone_id(),
            project_id(),
            sequence,
            code,
            "Domain",
            MilestoneState.PENDING,
            NOW,
            NOW,
        )


def test_milestone_rejects_missing_project_and_bad_time() -> None:
    with pytest.raises(DomainValidationError):
        Milestone(
            milestone_id(),
            "",  # type: ignore[arg-type]
            4,
            "M4",
            "Domain",
            MilestoneState.PENDING,
            LATER,
            NOW,
        )


def test_valid_job_accepts_zero_attempts_and_optional_milestone() -> None:
    job = Job(
        JobId.generate(),
        project_id(),
        "CODEX_RUN",
        JobState.QUEUED,
        0,
        NOW,
        NOW,
        milestone_id(),
    )
    assert job.attempt_count == 0
    assert job.milestone_id == milestone_id()
    assert (
        Job(
            JobId.generate(), project_id(), "DESIGN", JobState.QUEUED, 0, NOW, NOW
        ).milestone_id
        is None
    )


def test_job_rejects_negative_attempt_count() -> None:
    with pytest.raises(DomainValidationError):
        Job(JobId.generate(), project_id(), "CODEX_RUN", JobState.QUEUED, -1, NOW, NOW)


def test_valid_unresolved_gate_can_omit_response_times() -> None:
    gate = HumanGate(
        GateId.generate(), project_id(), GateType.HUMAN_TEST, GateState.PENDING, NOW
    )
    assert gate.responded_at is None
    assert gate.resolved_at is None


def test_gate_rejects_missing_project() -> None:
    with pytest.raises(DomainValidationError):
        HumanGate(
            GateId.generate(),
            "",  # type: ignore[arg-type]
            GateType.DESIGN_APPROVAL,
            GateState.PENDING,
            NOW,
        )


@pytest.mark.parametrize(
    ("responded", "resolved"),
    [
        (NOW - timedelta(seconds=1), None),
        (None, NOW - timedelta(seconds=1)),
        (LATER, NOW),
    ],
)
def test_gate_rejects_invalid_timestamp_order(
    responded: datetime | None, resolved: datetime | None
) -> None:
    with pytest.raises(DomainValidationError):
        HumanGate(
            GateId.generate(),
            project_id(),
            GateType.HUMAN_TEST,
            GateState.RESOLVED,
            NOW,
            responded_at=responded,
            resolved_at=resolved,
        )


def test_valid_workflow_event_handles_optional_relations_and_copies_payload() -> None:
    payload = {"result": "passed"}
    event = WorkflowEvent(
        WorkflowEventId.generate(),
        project_id(),
        "CI_PASSED",
        NOW,
        "corr-1",
        payload=payload,
    )
    payload["result"] = "changed"
    assert event.payload == {"result": "passed"}
    assert event.milestone_id is None
    with pytest.raises(TypeError):
        event.payload["extra"] = True


@pytest.mark.parametrize("event_type", ["", " "])
def test_workflow_event_rejects_empty_type(event_type: str) -> None:
    with pytest.raises(DomainValidationError):
        WorkflowEvent(
            WorkflowEventId.generate(), project_id(), event_type, NOW, "corr-1"
        )


def test_workflow_event_rejects_naive_timestamp() -> None:
    with pytest.raises(DomainValidationError):
        WorkflowEvent(
            WorkflowEventId.generate(),
            project_id(),
            "PROJECT_CREATED",
            NOW.replace(tzinfo=None),
            "corr-1",
        )


def test_domain_has_no_adapter_or_infrastructure_imports() -> None:
    domain_root = Path(__file__).parents[2] / "src" / "syntra_build" / "domain"
    forbidden = ("syntra_build.adapters", "syntra_build.infrastructure")
    for path in domain_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        ]
        assert not any(name.startswith(forbidden) for name in imported)
