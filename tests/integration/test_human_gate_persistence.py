import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.application.gates import CreateGateRequest, HumanGateService
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
