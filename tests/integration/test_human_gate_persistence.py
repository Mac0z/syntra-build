import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.application.commands import (
    Command,
    CommandAuditRequest,
    CommandParser,
    InboundMessage,
    ProjectCommandResult,
    ProjectResolution,
    ProjectSummary,
    ResolutionOutcome,
)
from syntra_build.application.commands.router import CommandRouter
from syntra_build.application.gates import (
    CreateGateRequest,
    HumanGateCommandHandler,
    HumanGateService,
)
from syntra_build.domain import (
    ExpectedResponseType,
    GateId,
    GateState,
    GateType,
    HumanGate,
    Project,
    ProjectId,
    ProjectState,
)
from syntra_build.infrastructure.persistence import (
    SQLiteHumanGateRepository,
    SQLiteProjectRepository,
    apply_migrations,
    open_database,
)
from syntra_build.infrastructure.persistence.errors import (
    ClosedGateError,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
PID = ProjectId(UUID(int=1))
GID = GateId(UUID(int=2))


class Notifier:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.messages: list[str] = []

    def send(self, text: str) -> str:
        if self.fail:
            raise RuntimeError("offline")
        self.messages.append(text)
        return "42"


class NoProjects:
    def list_projects(self) -> tuple[ProjectSummary, ...]:
        return ()

    def resolve_project(self, reference: str) -> ProjectResolution:
        return ProjectResolution(ResolutionOutcome.NOT_FOUND)

    def get_project_status(self, project_id: ProjectId) -> ProjectSummary:
        raise AssertionError("gate commands must not query projects")


class NoProjectCommands:
    def handle(self, command: Command, project: ProjectSummary) -> ProjectCommandResult:
        raise AssertionError(
            "gate commands must not mutate through M6 project commands"
        )


class NoAudit:
    def record(self, request: CommandAuditRequest) -> None:
        raise AssertionError("read-only waiting does not write command audit")


class Healthy:
    def current_health(self) -> str:
        return "healthy"


def routed(service: HumanGateService, text: str, message_id: str = "message-1") -> str:
    router = CommandRouter(
        project_queries=NoProjects(),
        project_commands=NoProjectCommands(),
        audit_sink=NoAudit(),
        health=Healthy(),
        gate_commands=HumanGateCommandHandler(service),
        parser=CommandParser(lambda: "command-correlation"),
    )
    return router.route(
        InboundMessage(
            "telegram",
            f"update-{message_id}",
            message_id,
            "7",
            NOW,
            text,
        )
    ).text


Setup = tuple[
    sqlite3.Connection, SQLiteHumanGateRepository, HumanGateService, HumanGate
]


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    db = open_database(tmp_path / "gates.db")
    apply_migrations(db)
    SQLiteProjectRepository(db, lambda: "transition").add(
        Project(PID, "FlowTrack", ProjectState.BUILDING, NOW, NOW)
    )
    repo = SQLiteHumanGateRepository(
        db,
        lambda: (
            "transition-"
            + str(db.execute("select count(*) from state_transitions").fetchone()[0])
        ),
    )
    service = HumanGateService(
        repo,
        gate_id_factory=lambda: GID,
        response_id_factory=lambda: "response-1",
        authorised_responder_ids=frozenset({"7"}),
    )
    gate = service.create(
        CreateGateRequest(
            PID,
            GateType.DESIGN_APPROVAL,
            "Design approval",
            "Approve design",
            ExpectedResponseType.DESIGN_APPROVAL,
            NOW,
            "SYSTEM",
            "corr",
        )
    )
    return db, repo, service, gate


def test_notification_failure_preserves_pending(setup: Setup) -> None:
    _, repo, service, gate = setup
    with pytest.raises(RuntimeError):
        service.notify(gate.id, Notifier(True), occurred_at=NOW)
    assert repo.get(gate.id).state is GateState.PENDING


def test_response_lifecycle_is_durable_and_duplicate_is_rejected(setup: Setup) -> None:
    db, repo, service, gate = setup
    notice = Notifier()
    gate = service.notify(gate.id, notice, occurred_at=NOW)
    assert gate.state is GateState.NOTIFIED and str(gate.id) in notice.messages[0]
    gate = service.respond(
        gate.id,
        project_id=PID,
        milestone_id=None,
        message_id="telegram-10",
        response_code="approve",
        response_text=None,
        responded_by="7",
        responded_at=NOW,
    )
    assert gate.state is GateState.RESOLVED and repo.responses(gate.id)[0].validated
    states = [
        r[0]
        for r in db.execute(
            "select new_state from state_transitions where gate_id=? order by rowid",
            (str(GID),),
        )
    ]
    assert states == ["NOTIFIED", "RESPONDED", "VALIDATED", "RESOLVED"]
    with pytest.raises(ClosedGateError):
        service.respond(
            gate.id,
            project_id=PID,
            milestone_id=None,
            message_id="telegram-10",
            response_code="APPROVE",
            response_text=None,
            responded_by="7",
            responded_at=NOW,
        )


def test_silence_and_outstanding_query_include_pending(setup: Setup) -> None:
    _, repo, _, gate = setup
    assert repo.outstanding() == (gate,)


def test_waiting_and_gate_response_route_end_to_end_through_sqlite(
    setup: Setup,
) -> None:
    _, repo, service, gate = setup
    service.notify(gate.id, Notifier(), occurred_at=NOW)
    waiting = routed(service, "waiting")
    assert "1 action need your attention" in waiting and str(gate.id) in waiting

    result = routed(service, f"gate {gate.id} APPROVE")
    assert result == f"Human gate {gate.id} resolved as APPROVE."
    assert repo.get(gate.id).state is GateState.RESOLVED
    assert routed(service, f"gate {gate.id} APPROVE", "message-2") == (
        "Human gate is already closed and cannot be answered."
    )


def test_unknown_gate_is_rejected_safely(setup: Setup) -> None:
    _, _, service, _ = setup
    unknown = GateId(UUID(int=99))
    assert routed(service, f"gate {unknown} APPROVE") == "Human gate not found."


@pytest.mark.parametrize(
    ("gate_type", "schema", "response_code"),
    [
        (
            GateType.DESIGN_APPROVAL,
            ExpectedResponseType.DESIGN_APPROVAL,
            "REQUEST_CHANGES",
        ),
        (GateType.HUMAN_TEST, ExpectedResponseType.HUMAN_TEST, "PASS"),
        (GateType.HUMAN_TEST, ExpectedResponseType.HUMAN_TEST, "FAIL"),
    ],
)
def test_command_surface_accepts_each_deterministic_response_schema(
    setup: Setup,
    gate_type: GateType,
    schema: ExpectedResponseType,
    response_code: str,
) -> None:
    _, repo, _, _ = setup
    gate_id = GateId(UUID(int=3))
    service = HumanGateService(
        repo,
        gate_id_factory=lambda: gate_id,
        response_id_factory=lambda: f"response-{response_code}",
        authorised_responder_ids=frozenset({"7"}),
    )
    gate = service.create(
        CreateGateRequest(
            PID,
            gate_type,
            "Action",
            "Choose",
            schema,
            NOW,
            "SYSTEM",
            "corr",
        )
    )
    service.notify(gate.id, Notifier(), occurred_at=NOW)
    assert routed(service, f"gate {gate.id} {response_code}") == (
        f"Human gate {gate.id} resolved as {response_code}."
    )
