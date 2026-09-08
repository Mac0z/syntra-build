"""Telegram transport bridge for application-level human-gate notification."""

from dataclasses import dataclass

from syntra_build.adapters.telegram.client import TelegramClient


@dataclass(frozen=True, slots=True)
class TelegramGateNotifier:
    """Send one gate notification through the existing bounded M5 gateway."""

    client: TelegramClient
    chat_id: int
    thread_id: int | None = None
    reply_to_message_id: int | None = None

    def send(self, text: str) -> str:
        sent = self.client.send_text(
            chat_id=self.chat_id,
            text=text,
            thread_id=self.thread_id,
            reply_to_message_id=self.reply_to_message_id,
        )
        return str(sent.message_id)
