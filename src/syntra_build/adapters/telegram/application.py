"""Thin authorized Telegram-to-application integration seam."""

from syntra_build.adapters.telegram.models import TelegramInboundMessage
from syntra_build.application.commands.models import CommandResponse, InboundMessage
from syntra_build.application.commands.router import CommandRouter


def to_application_message(message: TelegramInboundMessage) -> InboundMessage:
    """Convert an M5-authorized normalized message, not raw provider JSON."""
    return InboundMessage(
        source_platform="telegram",
        source_update_id=str(message.update_id),
        source_message_id=str(message.message_id),
        sender_id=str(message.user_id),
        received_at=message.received_at,
        text=message.text,
        chat_id=str(message.chat_id),
        thread_id=str(message.thread_id) if message.thread_id is not None else None,
        reply_to_message_id=(
            str(message.reply_to_message_id)
            if message.reply_to_message_id is not None
            else None
        ),
    )


def route_authorized_message(
    message: TelegramInboundMessage, router: CommandRouter
) -> CommandResponse:
    """Route only the authorized normalized records emitted by ``poll_updates``."""
    return router.route(to_application_message(message))
