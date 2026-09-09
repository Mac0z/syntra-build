"""Immutable provider-independent command contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class CommandType(StrEnum):
    CREATE_PROJECT = "CREATE_PROJECT"
    PING = "PING"
    HEALTH = "HEALTH"
    LIST_PROJECTS = "LIST_PROJECTS"
    PROJECT_STATUS = "PROJECT_STATUS"
    PAUSE_PROJECT = "PAUSE_PROJECT"
    RESUME_PROJECT = "RESUME_PROJECT"
    CANCEL_PROJECT = "CANCEL_PROJECT"
    WAITING = "WAITING"
    RESPOND_GATE = "RESPOND_GATE"


READ_ONLY_COMMANDS = frozenset(
    {
        CommandType.PING,
        CommandType.HEALTH,
        CommandType.LIST_PROJECTS,
        CommandType.PROJECT_STATUS,
        CommandType.WAITING,
    }
)
STATE_CHANGING_COMMANDS = frozenset(
    {
        CommandType.CREATE_PROJECT,
        CommandType.PAUSE_PROJECT,
        CommandType.RESUME_PROJECT,
        CommandType.CANCEL_PROJECT,
        CommandType.RESPOND_GATE,
    }
)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Authenticated message presented by a provider adapter."""

    source_platform: str
    source_update_id: str
    source_message_id: str
    sender_id: str
    received_at: datetime
    text: str
    chat_id: str | None = None
    thread_id: str | None = None
    reply_to_message_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "source_platform",
            "source_update_id",
            "source_message_id",
            "sender_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() != timedelta(
            0
        ):
            raise ValueError("received_at must be timezone-aware and in UTC")


@dataclass(frozen=True, slots=True)
class Command:
    type: CommandType
    requested_by: str
    requested_at: datetime
    source_platform: str
    source_update_id: str
    source_message_id: str
    correlation_id: str
    project_reference: str | None = None
    gate_reference: str | None = None
    gate_response: str | None = None
    project_name: str | None = None
    initial_request: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None

    @property
    def is_state_changing(self) -> bool:
        return self.type in STATE_CHANGING_COMMANDS


@dataclass(frozen=True, slots=True)
class CommandResponse:
    text: str
    correlation_id: str
    chat_id: str | None = None
    thread_id: str | None = None
    reply_to_message_id: str | None = None
