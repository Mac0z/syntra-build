"""Telegram transport bridge for application-level human-gate notification."""

import re
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
        match = re.search(r"Gate: ([0-9a-f-]{36})", text)
        markup = None
        if match and "design-package:" in text:
            gate = match.group(1)
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
        return str(sent.message_id)
