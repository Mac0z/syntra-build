"""Telegram inline controls for M25 human tests and decisions."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Final

from syntra_build.adapters.telegram.client import TelegramClient
from syntra_build.adapters.telegram.errors import TelegramAPIError, TelegramError
from syntra_build.adapters.telegram.models import TelegramCallbackQuery
from syntra_build.application.commands.models import Command, CommandType
from syntra_build.application.human_intervention import HumanInterventionService
from syntra_build.domain.gates import GateState, GateType
from syntra_build.domain.identifiers import GateId
from syntra_build.infrastructure.persistence.errors import PersistenceError
from syntra_build.infrastructure.persistence.gates import SQLiteHumanGateRepository
from syntra_build.infrastructure.persistence.telegram_interactions import (
    SQLiteTelegramGateNotificationRepository,
)

_CALLBACK: Final = re.compile(
    r"gate:(?:(pass|fail|blocked)|option:(0|[1-9]\d?)):"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
)
_LOGGER = logging.getLogger("syntra_build.adapters.telegram.human_intervention")


def human_gate_callback_data(
    action: str, gate_id: GateId, *, option_index: int | None = None
) -> str:
    if action == "option":
        if option_index is None or not 0 <= option_index < 100:
            raise ValueError("invalid Telegram gate option callback")
        value = f"gate:option:{option_index}:{gate_id}"
    else:
        if action not in {"pass", "fail", "blocked"} or option_index is not None:
            raise ValueError("invalid Telegram gate callback")
        value = f"gate:{action}:{gate_id}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("Telegram gate callback exceeds 64 bytes")
    return value


@dataclass(slots=True)
class TelegramHumanInterventionHandler:
    connection: sqlite3.Connection
    client: TelegramClient
    authorised_user_ids: frozenset[str]
    interventions: HumanInterventionService

    def handle_callback(self, callback: TelegramCallbackQuery) -> bool:
        """Handle only the M25 callback namespace using authoritative SQLite state."""
        match = _CALLBACK.fullmatch(callback.callback_data)
        if match is None:
            if callback.callback_data.startswith("gate:"):
                if str(callback.user_id) not in self.authorised_user_ids:
                    raise PermissionError("responder is not authorised")
                self._acknowledge(callback.callback_query_id, "Unsupported action.")
                return True
            return False
        if str(callback.user_id) not in self.authorised_user_ids:
            raise PermissionError("responder is not authorised")
        self._acknowledge(callback.callback_query_id)
        outcome, raw_index, raw_gate_id = match.groups()
        try:
            gate_id = GateId.from_string(raw_gate_id)
            SQLiteTelegramGateNotificationRepository(self.connection).validate_callback(
                gate_id,
                str(callback.chat_id),
                str(callback.thread_id) if callback.thread_id is not None else None,
                str(callback.source_message_id),
            )
            gate = SQLiteHumanGateRepository(self.connection, lambda: "unused").get(
                gate_id
            )
            if gate.state is not GateState.NOTIFIED:
                raise PersistenceError("human gate is not answerable")
            if outcome is not None:
                if gate.gate_type is not GateType.HUMAN_TEST:
                    raise PersistenceError("test callback does not match gate type")
                response = outcome.upper()
            else:
                if gate.gate_type not in {
                    GateType.PRODUCT_DECISION,
                    GateType.TECHNICAL_DECISION,
                }:
                    raise PersistenceError("option callback does not match gate type")
                index = int(raw_index)
                if index >= len(gate.options):
                    raise PersistenceError("gate option callback is invalid")
                response = gate.options[index]
            result = self.interventions.respond(
                Command(
                    CommandType.RESPOND_GATE,
                    str(callback.user_id),
                    callback.received_at,
                    "telegram",
                    str(callback.update_id),
                    f"callback:{callback.callback_query_id}",
                    f"telegram:{callback.update_id}",
                    gate_reference=str(gate_id),
                    gate_response=response,
                    chat_id=str(callback.chat_id),
                    thread_id=str(callback.thread_id)
                    if callback.thread_id is not None
                    else None,
                ),
                gate,
            )
            self.client.send_text(
                chat_id=callback.chat_id,
                text=result,
                thread_id=callback.thread_id,
                reply_to_message_id=callback.source_message_id,
            )
        except PersistenceError, ValueError:
            self.client.send_text(
                chat_id=callback.chat_id,
                text="This human action is invalid, stale, or already resolved.",
                thread_id=callback.thread_id,
                reply_to_message_id=callback.source_message_id,
            )
        return True

    def _acknowledge(self, callback_query_id: str, text: str | None = None) -> None:
        try:
            self.client.answer_callback(callback_query_id, text)
        except TelegramAPIError as error:
            if not error.is_terminal_callback_acknowledgement:
                raise
            _LOGGER.warning(
                "Telegram callback acknowledgement expired",
                extra={"event": "telegram_callback_acknowledgement_terminal"},
            )
        except TelegramError:
            raise
