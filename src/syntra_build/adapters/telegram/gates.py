"""Telegram transport bridge for application-level human-gate notification."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from syntra_build.adapters.telegram.client import TelegramClient
from syntra_build.domain import GateId
from syntra_build.infrastructure.persistence import (
    SQLiteTelegramGateNotificationRepository,
)


@dataclass(frozen=True, slots=True)
class TelegramGateNotifier:
    """Send one gate notification through the existing bounded M5 gateway."""

    client: TelegramClient
    chat_id: int
    thread_id: int | None = None
    reply_to_message_id: int | None = None
    notifications: SQLiteTelegramGateNotificationRepository | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def send(self, text: str) -> str:
        match = re.search(r"Gate: ([0-9a-f-]{36})", text)
        markup = None
        if match and "design-package:" in text:
            gate = match.group(1)
            if self.notifications is not None:
                existing = self.notifications.find(GateId.from_string(gate))
                if existing is not None:
                    expected_thread = (
                        str(self.thread_id) if self.thread_id is not None else None
                    )
                    if (
                        existing.chat_id != str(self.chat_id)
                        or existing.thread_id != expected_thread
                    ):
                        raise ValueError(
                            "existing Telegram gate notification destination conflicts"
                        )
                    return existing.message_id
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "View SPEC", "callback_data": f"design:spec:{gate}"},
                        {
                            "text": "View AGENTS",
                            "callback_data": f"design:agents:{gate}",
                        },
                    ],
                    [
                        {"text": "Approve", "callback_data": f"design:approve:{gate}"},
                        {
                            "text": "Request changes",
                            "callback_data": f"design:changes:{gate}",
                        },
                    ],
                ]
            }
        sent = self.client.send_text(
            chat_id=self.chat_id,
            text=text,
            thread_id=self.thread_id,
            reply_to_message_id=self.reply_to_message_id,
            reply_markup=markup,
        )
        if match and self.notifications is not None:
            self.notifications.add(
                GateId.from_string(match.group(1)),
                str(sent.chat_id),
                str(sent.thread_id) if sent.thread_id is not None else None,
                str(sent.message_id),
                self.clock(),
            )
        return str(sent.message_id)
