from datetime import UTC, datetime

import pytest

from syntra_build.application.commands import CommandParser, CommandType, InboundMessage
from syntra_build.application.projects import (
    ProjectCreationError,
    ProjectCreationFailure,
    canonicalize_project_name,
    validate_project_name,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def message(text: str) -> InboundMessage:
    return InboundMessage("telegram", "10", "20", "30", NOW, text, "40", "50")


@pytest.mark.parametrize("prefix", ["/create", "create", "/create@SyntraBuildBot"])
def test_create_parser_preserves_structured_values_and_source(prefix: str) -> None:
    result = CommandParser(lambda: "correlation").parse(
        message(f'{prefix}  "Flow Track"  |  Build a desktop tracker  ')
    )
    assert result.command is not None
    assert result.command.type is CommandType.CREATE_PROJECT
    assert result.command.project_name == "Flow Track"
    assert result.command.initial_request == "Build a desktop tracker"
    assert (result.command.chat_id, result.command.thread_id) == ("40", "50")


@pytest.mark.parametrize(
    "text", ["create", "create Flow", "create | request", "create Flow |"]
)
def test_malformed_create_is_rejected(text: str) -> None:
    assert CommandParser().parse(message(text)).command is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("FlowTrack", "flowtrack"),
        ("FLOWTRACK", "flowtrack"),
        ("  Flow Track  ", "flow-track"),
        ("Flow_Track!!", "flow-track"),
    ],
)
def test_canonicalization_is_stable(value: str, expected: str) -> None:
    assert canonicalize_project_name(value) == expected


@pytest.mark.parametrize("value", ["!!!", "x" * 101, "bad\x00name"])
def test_invalid_names_are_rejected(value: str) -> None:
    with pytest.raises(ProjectCreationError) as raised:
        validate_project_name(value)
    assert raised.value.failure is ProjectCreationFailure.INVALID_NAME


@pytest.mark.parametrize("value", ["Syntra Build", "SYSTEM", "internal"])
def test_reserved_names_are_rejected(value: str) -> None:
    with pytest.raises(ProjectCreationError) as raised:
        validate_project_name(value)
    assert raised.value.failure is ProjectCreationFailure.RESERVED_NAME
