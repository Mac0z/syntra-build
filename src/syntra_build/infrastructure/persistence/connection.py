"""Explicit SQLite connection and transaction management."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from syntra_build.infrastructure.persistence.errors import (
    DatabaseConnectionError,
    PersistenceError,
    TransactionError,
)

BUSY_TIMEOUT_MILLISECONDS: Final = 5_000
_LOGGER = logging.getLogger(__name__)


def open_database(path: Path) -> sqlite3.Connection:
    """Open an explicitly owned, configured connection to a file database."""
    try:
        connection = sqlite3.connect(
            path, timeout=BUSY_TIMEOUT_MILLISECONDS / 1_000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if mode is None or str(mode[0]).casefold() != "wal":
            connection.close()
            raise DatabaseConnectionError(
                "database could not be configured for WAL mode"
            )
    except DatabaseConnectionError:
        raise
    except sqlite3.Error as error:
        raise DatabaseConnectionError(
            "database could not be opened or configured"
        ) from error

    _LOGGER.info("Database opened", extra={"event": "database_opened"})
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Commit one atomic block, rollback on failure, and reject nesting."""
    if connection.in_transaction:
        raise TransactionError("nested transactions are not supported")
    try:
        connection.execute("BEGIN")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise PersistenceError("database transaction failed") from error
