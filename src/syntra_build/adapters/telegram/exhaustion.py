"""Telegram implementation of the provider-neutral exhaustion notifier."""

from dataclasses import dataclass

from syntra_build.adapters.telegram.client import TelegramClient
from syntra_build.application.retries import ExhaustionNotice


@dataclass(frozen=True, slots=True)
class TelegramExhaustionNotifier:
    client: TelegramClient
    chat_id: int
    thread_id: int | None = None

    def notify(self, notice: ExhaustionNotice) -> None:
        self.client.send_text(
            chat_id=self.chat_id, text=notice.message(), thread_id=self.thread_id
        )
