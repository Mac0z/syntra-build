from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from inspect import signature
from uuid import UUID

import pytest

from syntra_build.adapters.telegram import TelegramInboundMessage
from syntra_build.adapters.telegram.application import (
    route_authorized_message,
    to_application_message,
)
from syntra_build.application.commands import (
    Command,
    CommandAuditRequest,
    CommandParser,
    CommandType,
    InboundMessage,
    ProjectCommandResult,
    ProjectResolution,
    ProjectSummary,
    ResolutionOutcome,
)
from syntra_build.application.commands.router import HELP_TEXT, CommandRouter
from syntra_build.domain import ProjectId, ProjectState

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
FLOW_ID = ProjectId(UUID("00000000-0000-0000-0000-000000000001"))
OTHER_ID = ProjectId(UUID("00000000-0000-0000-0000-000000000002"))
FLOW = ProjectSummary(FLOW_ID, "FlowTrack", ProjectState.BUILDING)
OTHER = ProjectSummary(OTHER_ID, "Other", ProjectState.PAUSED)


@dataclass
class FakeQueries:
    projects: tuple[ProjectSummary, ...] = ()
    resolution: ProjectResolution = ProjectResolution(ResolutionOutcome.NOT_FOUND)
    calls: list[tuple[str, object]] = field(default_factory=list)

    def list_projects(self) -> tuple[ProjectSummary, ...]:
        self.calls.append(("list", None))
        return self.projects

    def resolve_project(self, reference: str) -> ProjectResolution:
        self.calls.append(("resolve", reference))
        return self.resolution

    def get_project_status(self, project_id: ProjectId) -> ProjectSummary:
        self.calls.append(("status", project_id))
        assert self.resolution.project is not None
        return self.resolution.project


@dataclass
class FakeCommands:
    result: ProjectCommandResult = ProjectCommandResult(
        "Project command handling is not available.", False
    )
    calls: list[tuple[Command, ProjectSummary]] = field(default_factory=list)

    def handle(self, command: Command, project: ProjectSummary) -> ProjectCommandResult:
        self.calls.append((command, project))
        return self.result


@dataclass
class FakeAudit:
    requests: list[CommandAuditRequest] = field(default_factory=list)

    def record(self, request: CommandAuditRequest) -> None:
        self.requests.append(request)


@dataclass
class FakeHealth:
    value: str = "routing: healthy"
    calls: int = 0

    def current_health(self) -> str:
        self.calls += 1
        return self.value


@dataclass
class FutureIntentResolver:
    """Future-facing protocol implementation that M6 deliberately never wires in."""

    calls: int = 0

    def resolve(self, message: InboundMessage) -> Command | None:
        self.calls += 1
        raise AssertionError(
            f"M6 must not interpret unknown input from {message.sender_id}"
        )


def inbound(text: str = "ping") -> InboundMessage:
    return InboundMessage(
        source_platform="test-provider",
        source_update_id="update-private-marker",
        source_message_id="message-2",
        sender_id="human-3",
        received_at=NOW,
        text=text,
        chat_id="chat-4",
        thread_id="thread-5",
    )


def make_router(
    *,
    queries: FakeQueries | None = None,
    commands: FakeCommands | None = None,
    audit: FakeAudit | None = None,
    health: FakeHealth | None = None,
) -> tuple[CommandRouter, FakeQueries, FakeCommands, FakeAudit, FakeHealth]:
    query_service = queries or FakeQueries()
    command_service = commands or FakeCommands()
    audit_sink = audit or FakeAudit()
    health_service = health or FakeHealth()
    return (
        CommandRouter(
            project_queries=query_service,
            project_commands=command_service,
            audit_sink=audit_sink,
            health=health_service,
            parser=CommandParser(lambda: "correlation-6"),
        ),
        query_service,
        command_service,
        audit_sink,
        health_service,
    )


def test_ping_is_local_and_exact() -> None:
    router, queries, commands, audit, health = make_router()
    response = router.route(inbound())
    assert response.text == "pong"
    assert response.correlation_id == "correlation-6"
    assert response.chat_id == "chat-4"
    assert response.thread_id == "thread-5"
    assert response.reply_to_message_id == "message-2"
    assert not queries.calls and not commands.calls and not audit.requests
    assert health.calls == 0


def test_health_uses_only_injected_local_view() -> None:
    health = FakeHealth("application routing: ready")
    router, queries, commands, audit, _ = make_router(health=health)
    assert router.route(inbound("health")).text == "application routing: ready"
    assert health.calls == 1
    assert not queries.calls and not commands.calls and not audit.requests


@pytest.mark.parametrize(
    ("projects", "expected"),
    [
        ((), "No projects."),
        ((FLOW,), "Projects:\nFlowTrack — BUILDING"),
        ((FLOW, OTHER), "Projects:\nFlowTrack — BUILDING\nOther — PAUSED"),
    ],
)
def test_projects_are_human_readable(
    projects: tuple[ProjectSummary, ...], expected: str
) -> None:
    router, *_ = make_router(queries=FakeQueries(projects=projects))
    assert router.route(inbound("projects")).text == expected


@pytest.mark.parametrize("reference", [str(FLOW_ID), "FlowTrack"])
def test_status_resolves_id_or_exact_canonical_name(reference: str) -> None:
    queries = FakeQueries(resolution=ProjectResolution(ResolutionOutcome.FOUND, FLOW))
    router, _, commands, audit, _ = make_router(queries=queries)
    assert router.route(inbound(f"status {reference}")).text == (
        "Project: FlowTrack\nState: BUILDING"
    )
    assert queries.calls == [("resolve", reference), ("status", FLOW_ID)]
    assert not commands.calls and not audit.requests


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ResolutionOutcome.NOT_FOUND, "Project not found: Flow"),
        (ResolutionOutcome.AMBIGUOUS, "Project reference is ambiguous: Flow"),
        (ResolutionOutcome.INVALID, "Project reference is invalid: Flow"),
    ],
)
def test_unresolved_project_does_not_mutate_or_audit(
    outcome: ResolutionOutcome, expected: str
) -> None:
    router, _, commands, audit, _ = make_router(
        queries=FakeQueries(resolution=ProjectResolution(outcome))
    )
    assert router.route(inbound("pause Flow")).text == expected
    assert not commands.calls and not audit.requests


@pytest.mark.parametrize(
    ("verb", "kind"),
    [
        ("pause", CommandType.PAUSE_PROJECT),
        ("resume", CommandType.RESUME_PROJECT),
        ("cancel", CommandType.CANCEL_PROJECT),
    ],
)
def test_state_change_is_audited_then_delegated_with_source_identity(
    verb: str, kind: CommandType
) -> None:
    queries = FakeQueries(resolution=ProjectResolution(ResolutionOutcome.FOUND, FLOW))
    commands = FakeCommands(ProjectCommandResult("Request accepted durably.", True))
    audit = FakeAudit()
    router, *_ = make_router(queries=queries, commands=commands, audit=audit)

    assert (
        router.route(inbound(f"{verb} FlowTrack")).text == "Request accepted durably."
    )
    command, project = commands.calls[0]
    assert project is FLOW
    assert command.type is kind and command.is_state_changing
    assert command.requested_by == "human-3"
    assert command.requested_at == NOW
    assert command.source_platform == "test-provider"
    assert command.source_update_id == "update-private-marker"
    assert command.source_message_id == "message-2"
    assert command.correlation_id == "correlation-6"
    assert audit.requests == [
        CommandAuditRequest("STATE_CHANGE_COMMAND_REQUESTED", command, FLOW_ID)
    ]


def test_unavailable_service_does_not_claim_success_or_mutate_project() -> None:
    queries = FakeQueries(resolution=ProjectResolution(ResolutionOutcome.FOUND, FLOW))
    router, _, commands, _, _ = make_router(queries=queries)
    before = FLOW.state
    response = router.route(inbound("cancel FlowTrack"))
    assert response.text == "Project command handling is not available."
    assert commands.result.performed is False
    assert FLOW.state is before


@pytest.mark.parametrize(
    "text", ["status", "pause", "resume", "cancel", "ping extra", "unknown"]
)
def test_invalid_input_returns_help_without_services(text: str) -> None:
    router, queries, commands, audit, health = make_router()
    assert router.route(inbound(text)).text == HELP_TEXT
    assert not queries.calls and not commands.calls and not audit.requests
    assert health.calls == 0


def test_unknown_input_does_not_invoke_future_intent_resolution() -> None:
    future_resolver = FutureIntentResolver()
    router, queries, commands, audit, health = make_router()

    assert router.route(inbound("what is happening with FlowTrack")).text == HELP_TEXT

    assert "intent_resolver" not in signature(CommandRouter).parameters
    assert future_resolver.calls == 0
    assert not queries.calls and not commands.calls and not audit.requests
    assert health.calls == 0


def test_duplicate_source_is_neutral_and_routes_twice() -> None:
    queries = FakeQueries(resolution=ProjectResolution(ResolutionOutcome.FOUND, FLOW))
    router, _, commands, audit, _ = make_router(queries=queries)
    source = inbound("pause FlowTrack")
    router.route(source)
    router.route(source)
    assert len(commands.calls) == 2
    assert len(audit.requests) == 2


def test_logs_include_identity_not_unrestricted_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router, *_ = make_router()
    logger = logging.getLogger("syntra_build.application.commands")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)
    try:
        router.route(inbound("unknown arbitrary-sensitive-body"))
    finally:
        logger.removeHandler(caplog.handler)
    assert any(
        getattr(record, "metadata", {}).get("source_update_id")
        == "update-private-marker"
        and getattr(record, "metadata", {}).get("source_message_id") == "message-2"
        for record in caplog.records
    )
    assert "arbitrary-sensitive-body" not in caplog.text


def test_telegram_bridge_preserves_authorized_normalized_identity() -> None:
    telegram = TelegramInboundMessage(
        update_id=70,
        message_id=80,
        chat_id=90,
        user_id=100,
        text="/ping",
        received_at=NOW,
        thread_id=110,
        reply_to_message_id=120,
    )
    neutral = to_application_message(telegram)
    assert neutral == InboundMessage(
        source_platform="telegram",
        source_update_id="70",
        source_message_id="80",
        sender_id="100",
        received_at=NOW,
        text="/ping",
        chat_id="90",
        thread_id="110",
        reply_to_message_id="120",
    )
    router, *_ = make_router()
    assert route_authorized_message(telegram, router).text == "pong"
