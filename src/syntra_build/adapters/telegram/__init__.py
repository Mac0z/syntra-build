"""Telegram Bot API transport adapter."""

from syntra_build.adapters.telegram.client import HTTPResponse, TelegramClient
from syntra_build.adapters.telegram.design_approval import (
    TelegramDesignApprovalHandler,
    design_callback_data,
    markdown_pdf,
)
from syntra_build.adapters.telegram.errors import (
    TelegramAPIError,
    TelegramConfigurationError,
    TelegramError,
    TelegramProtocolError,
    TelegramTransportError,
)
from syntra_build.adapters.telegram.gates import TelegramGateNotifier
from syntra_build.adapters.telegram.models import (
    TelegramCallbackQuery,
    TelegramInboundMessage,
    TelegramPolledUpdate,
    TelegramSentMessage,
    TelegramUpdateDisposition,
)

__all__ = [
    "HTTPResponse",
    "TelegramAPIError",
    "TelegramClient",
    "TelegramConfigurationError",
    "TelegramError",
    "TelegramInboundMessage",
    "TelegramCallbackQuery",
    "TelegramPolledUpdate",
    "TelegramGateNotifier",
    "TelegramDesignApprovalHandler",
    "design_callback_data",
    "markdown_pdf",
    "TelegramProtocolError",
    "TelegramSentMessage",
    "TelegramUpdateDisposition",
    "TelegramTransportError",
]
