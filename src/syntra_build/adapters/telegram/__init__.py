"""Telegram Bot API transport adapter."""

from syntra_build.adapters.telegram.client import HTTPResponse, TelegramClient
from syntra_build.adapters.telegram.errors import (
    TelegramAPIError,
    TelegramConfigurationError,
    TelegramError,
    TelegramProtocolError,
    TelegramTransportError,
)
from syntra_build.adapters.telegram.gates import TelegramGateNotifier
from syntra_build.adapters.telegram.models import (
    TelegramInboundMessage,
    TelegramSentMessage,
)

__all__ = [
    "HTTPResponse",
    "TelegramAPIError",
    "TelegramClient",
    "TelegramConfigurationError",
    "TelegramError",
    "TelegramInboundMessage",
    "TelegramGateNotifier",
    "TelegramProtocolError",
    "TelegramSentMessage",
    "TelegramTransportError",
]
