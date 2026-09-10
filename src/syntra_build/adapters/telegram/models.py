"""Normalised, immutable Telegram transport records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class TelegramInboundMessage:
    """An authorised text message, isolated from Telegram's raw JSON shape."""

    update_id: int
    message_id: int
    chat_id: int
    user_id: int
    text: str
    received_at: datetime
    thread_id: int | None = None
    reply_to_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramCallbackQuery:
    """An authorised callback, kept distinct from inbound text."""

    update_id: int
    callback_query_id: str
    user_id: int
    chat_id: int
    source_message_id: int
    callback_data: str
    received_at: datetime
    thread_id: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramSentMessage:
    """Stable reference to a text message accepted by Telegram."""

    message_id: int
    chat_id: int
    thread_id: int | None = None


class TelegramUpdateDisposition(StrEnum):
    """Security-boundary decision for a provider update."""

    ROUTABLE = "ROUTABLE"
    UNAUTHORISED = "UNAUTHORISED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class TelegramPolledUpdate:
    """One ordered provider update and its safe routing disposition."""

    update_id: int
    disposition: TelegramUpdateDisposition
    message: TelegramInboundMessage | None = None
    callback: TelegramCallbackQuery | None = None

    def __post_init__(self) -> None:
        payloads = int(self.message is not None) + int(self.callback is not None)
        if (self.disposition is TelegramUpdateDisposition.ROUTABLE) != (payloads == 1):
            raise ValueError("routable Telegram updates require exactly one payload")
