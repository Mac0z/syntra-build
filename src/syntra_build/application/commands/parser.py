"""Deterministic parsing for the M6 command vocabulary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from syntra_build.application.commands.models import (
    Command,
    CommandType,
    InboundMessage,
)
from syntra_build.infrastructure.logging import new_correlation_id


class ParseFailure(StrEnum):
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"
    MALFORMED = "MALFORMED"


@dataclass(frozen=True, slots=True)
class ParseResult:
    command: Command | None = None
    failure: ParseFailure | None = None


_COMMANDS = {
    "ping": (CommandType.PING, False),
    "health": (CommandType.HEALTH, False),
    "projects": (CommandType.LIST_PROJECTS, False),
    "status": (CommandType.PROJECT_STATUS, True),
    "pause": (CommandType.PAUSE_PROJECT, True),
    "resume": (CommandType.RESUME_PROJECT, True),
    "cancel": (CommandType.CANCEL_PROJECT, True),
}


class CommandParser:
    def __init__(self, correlation_id_factory: Callable[[], str] = new_correlation_id):
        self._correlation_id_factory = correlation_id_factory

    def parse(self, message: InboundMessage) -> ParseResult:
        stripped = message.text.strip()
        if not stripped:
            return ParseResult(failure=ParseFailure.EMPTY)
        parts = stripped.split(maxsplit=1)
        head = parts[0]
        if head.startswith("/"):
            # Telegram's optional bot suffix is syntax, not an identity decision.
            token = head[1:].partition("@")[0].casefold()
        else:
            token = head.casefold()
        definition = _COMMANDS.get(token)
        if definition is None:
            return ParseResult(failure=ParseFailure.UNKNOWN)
        command_type, needs_project = definition
        argument = parts[1].strip() if len(parts) == 2 else ""
        if needs_project == (not argument):
            return ParseResult(failure=ParseFailure.MALFORMED)
        return ParseResult(
            command=Command(
                type=command_type,
                project_reference=argument or None,
                requested_by=message.sender_id,
                requested_at=message.received_at,
                source_platform=message.source_platform,
                source_update_id=message.source_update_id,
                source_message_id=message.source_message_id,
                correlation_id=self._correlation_id_factory(),
            )
        )
