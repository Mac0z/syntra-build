# ruff: noqa: E501
"""Durable correlation for Telegram design-feedback prompts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from syntra_build.domain import DesignPackageId, GateId
from syntra_build.infrastructure.persistence.errors import PersistenceError


class TelegramInteractionState(StrEnum):
    PROMPTING = "PROMPTING"
    WAITING_FEEDBACK = "WAITING_FEEDBACK"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class TelegramGateInteraction:
    id: str
    gate_id: GateId
    package_id: DesignPackageId
    chat_id: str
    user_id: str
    thread_id: str | None
    prompt_message_id: str | None
    state: TelegramInteractionState
    created_at: datetime
    resolved_at: datetime | None = None


class SQLiteTelegramGateInteractionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def begin(
        self,
        gate_id: GateId,
        package_id: DesignPackageId,
        chat_id: str,
        user_id: str,
        thread_id: str | None,
        created_at: datetime,
    ) -> TelegramGateInteraction:
        existing = self.active(gate_id, user_id)
        if existing:
            return existing
        try:
            interaction_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO telegram_gate_interactions(id,gate_id,package_id,chat_id,user_id,thread_id,state,created_at) VALUES(?,?,?,?,?,?,'PROMPTING',?)",
                (
                    interaction_id,
                    str(gate_id),
                    str(package_id),
                    chat_id,
                    user_id,
                    thread_id,
                    created_at.isoformat(timespec="microseconds"),
                ),
            )
            return self._get(interaction_id)
        except sqlite3.Error as error:
            raise PersistenceError(
                "Telegram feedback interaction could not be created"
            ) from error

    def activate(
        self, interaction_id: str, prompt_message_id: str
    ) -> TelegramGateInteraction:
        if (
            self.connection.execute(
                "UPDATE telegram_gate_interactions SET state='WAITING_FEEDBACK',prompt_message_id=? WHERE id=? AND state='PROMPTING'",
                (prompt_message_id, interaction_id),
            ).rowcount
            != 1
        ):
            raise PersistenceError("Telegram feedback interaction is not promptable")
        return self._get(interaction_id)

    def for_reply(
        self, chat_id: str, user_id: str, prompt_message_id: str
    ) -> TelegramGateInteraction | None:
        row = self.connection.execute(
            "SELECT id FROM telegram_gate_interactions WHERE chat_id=? AND user_id=? AND prompt_message_id=? AND state='WAITING_FEEDBACK'",
            (chat_id, user_id, prompt_message_id),
        ).fetchone()
        return self._get(row["id"]) if row else None

    def resolve(self, interaction_id: str, at: datetime) -> None:
        if (
            self.connection.execute(
                "UPDATE telegram_gate_interactions SET state='RESOLVED',resolved_at=? WHERE id=? AND state='WAITING_FEEDBACK'",
                (at.isoformat(timespec="microseconds"), interaction_id),
            ).rowcount
            != 1
        ):
            raise PersistenceError("Telegram feedback interaction was already resolved")

    def active(self, gate_id: GateId, user_id: str) -> TelegramGateInteraction | None:
        row = self.connection.execute(
            "SELECT id FROM telegram_gate_interactions WHERE gate_id=? AND user_id=? AND state IN ('PROMPTING','WAITING_FEEDBACK')",
            (str(gate_id), user_id),
        ).fetchone()
        return self._get(row["id"]) if row else None

    def _get(self, interaction_id: str) -> TelegramGateInteraction:
        row = self.connection.execute(
            "SELECT * FROM telegram_gate_interactions WHERE id=?", (interaction_id,)
        ).fetchone()
        if row is None:
            raise PersistenceError("Telegram feedback interaction does not exist")
        return TelegramGateInteraction(
            row["id"],
            GateId.from_string(row["gate_id"]),
            DesignPackageId.from_string(row["package_id"]),
            row["chat_id"],
            row["user_id"],
            row["thread_id"],
            row["prompt_message_id"],
            TelegramInteractionState(row["state"]),
            datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            datetime.fromisoformat(row["resolved_at"]).astimezone(UTC)
            if row["resolved_at"]
            else None,
        )
