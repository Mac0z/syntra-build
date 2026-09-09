"""Durable, monotonic cursors for ordered external providers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from syntra_build.infrastructure.persistence.connection import transaction_scope


class SQLiteProviderCursorRepository:
    """Read and monotonically advance provider update cursors."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._connection = connection
        self._clock = clock

    def get(self, provider: str) -> int | None:
        row = self._connection.execute(
            "SELECT last_processed_update_id FROM provider_cursors WHERE provider=?",
            (provider,),
        ).fetchone()
        return None if row is None else int(row[0])

    def advance(self, provider: str, update_id: int) -> int:
        """Persist the greatest observed ID without allowing stale writers backward."""
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        with transaction_scope(self._connection):
            self._connection.execute(
                """INSERT INTO provider_cursors
                   (provider,last_processed_update_id,updated_at) VALUES (?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET
                     last_processed_update_id=excluded.last_processed_update_id,
                     updated_at=excluded.updated_at
                   WHERE excluded.last_processed_update_id
                         > provider_cursors.last_processed_update_id""",
                (provider, update_id, timestamp.astimezone(UTC).isoformat()),
            )
            stored = self.get(provider)
        assert stored is not None
        return stored
