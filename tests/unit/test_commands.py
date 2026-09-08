from datetime import UTC, datetime

import pytest

from syntra_build.application.commands import (
    READ_ONLY_COMMANDS,
    STATE_CHANGING_COMMANDS,
    CommandParser,
    CommandType,
    InboundMessage,
    ParseFailure,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def message(text: str) -> InboundMessage:
    return InboundMessage(
        source_platform="test",
        source_update_id="update-7",
        source_message_id="message-8",
        sender_id="human-9",
        received_at=NOW,
        text=text,
    )


@pytest.mark.parametrize(
    ("text", "expected", "reference"),
    [
        ("ping", CommandType.PING, None),
        ("/ping", CommandType.PING, None),
        ("PING", CommandType.PING, None),
        ("/ping@SyntraBuildBot", CommandType.PING, None),
        ("health", CommandType.HEALTH, None),
        ("/health", CommandType.HEALTH, None),
        ("projects", CommandType.LIST_PROJECTS, None),
        ("/projects", CommandType.LIST_PROJECTS, None),
        ("status FlowTrack", CommandType.PROJECT_STATUS, "FlowTrack"),
        ("/status FlowTrack", CommandType.PROJECT_STATUS, "FlowTrack"),
        ("pause FlowTrack", CommandType.PAUSE_PROJECT, "FlowTrack"),
        ("resume FlowTrack", CommandType.RESUME_PROJECT, "FlowTrack"),
        ("cancel FlowTrack", CommandType.CANCEL_PROJECT, "FlowTrack"),
        ("  StAtUs   Flow Track  ", CommandType.PROJECT_STATUS, "Flow Track"),
    ],
)
def test_parser_returns_typed_commands(
    text: str, expected: CommandType, reference: str | None
) -> None:
    result = CommandParser(lambda: "correlation-1").parse(message(text))

    assert result.failure is None
    assert result.command is not None
    assert result.command.type is expected
    assert result.command.project_reference == reference
    assert result.command.correlation_id == "correlation-1"
    assert result.command.source_update_id == "update-7"
    assert result.command.source_message_id == "message-8"


@pytest.mark.parametrize("text", ["status", "pause", "resume", "cancel"])
def test_missing_argument_is_malformed(text: str) -> None:
    assert CommandParser().parse(message(text)).failure is ParseFailure.MALFORMED


@pytest.mark.parametrize("text", ["ping extra", "health nonsense", "projects all"])
def test_unexpected_argument_is_malformed(text: str) -> None:
    assert CommandParser().parse(message(text)).failure is ParseFailure.MALFORMED


def test_unknown_and_empty_inputs_are_distinct() -> None:
    parser = CommandParser()
    assert parser.parse(message("deploy FlowTrack")).failure is ParseFailure.UNKNOWN
    assert parser.parse(message(" \t ")).failure is ParseFailure.EMPTY


def test_mutability_classification_is_explicit_and_complete() -> None:
    assert READ_ONLY_COMMANDS == {
        CommandType.PING,
        CommandType.HEALTH,
        CommandType.LIST_PROJECTS,
        CommandType.PROJECT_STATUS,
    }
    assert STATE_CHANGING_COMMANDS == {
        CommandType.PAUSE_PROJECT,
        CommandType.RESUME_PROJECT,
        CommandType.CANCEL_PROJECT,
    }
    assert READ_ONLY_COMMANDS.isdisjoint(STATE_CHANGING_COMMANDS)


def test_inbound_message_requires_utc() -> None:
    with pytest.raises(ValueError, match="UTC"):
        InboundMessage("test", "1", "2", "3", datetime(2026, 1, 1), "ping")
