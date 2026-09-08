"""Small, ordered migration runner for Syntra Build's SQLite schema."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import MigrationError

_LOGGER = logging.getLogger(__name__)
_MIGRATION_TABLE = "schema_migrations"


@dataclass(frozen=True, slots=True)
class Migration:
    """One source-controlled schema change consisting only of trusted SQL."""

    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="001_initial",
        statements=(
            """CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            ) STRICT""",
        ),
    ),
)


def validate_migrations(migrations: Sequence[Migration]) -> None:
    """Require positive, contiguous versions in their declared order."""
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError(
            "migration versions must be unique, contiguous, and ordered from 1"
        )
    if any(not item.name or not item.statements for item in migrations):
        raise MigrationError("each migration requires a name and SQL statements")


def _migration_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", _MIGRATION_TABLE),
    ).fetchone()
    return row is not None


def applied_migrations(connection: sqlite3.Connection) -> tuple[tuple[int, str], ...]:
    """Return ordered migration history, or an empty history initially."""
    if not _migration_table_exists(connection):
        return ()
    try:
        rows = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.Error as error:
        raise MigrationError("migration history could not be read") from error
    return tuple((int(row[0]), str(row[1])) for row in rows)


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Return the latest applied schema version, using zero for a new database."""
    history = applied_migrations(connection)
    return history[-1][0] if history else 0


def _validate_history(
    history: Sequence[tuple[int, str]], migrations: Sequence[Migration]
) -> None:
    if len(history) > len(migrations):
        raise MigrationError("database schema is newer than this application")
    expected = tuple((item.version, item.name) for item in migrations[: len(history)])
    if tuple(history) != expected:
        raise MigrationError("database migration history is unknown or inconsistent")


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> int:
    """Validate history and atomically apply each outstanding migration."""
    validate_migrations(migrations)
    history = applied_migrations(connection)
    _validate_history(history, migrations)

    for migration in migrations[len(history) :]:
        metadata = {"version": migration.version, "name": migration.name}
        _LOGGER.info(
            "Migration started",
            extra={"event": "migration_started", "metadata": metadata},
        )
        try:
            with transaction(connection):
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (migration.version, migration.name),
                )
        except Exception as error:
            raise MigrationError(
                f"migration {migration.version} could not be applied"
            ) from error
        _LOGGER.info(
            "Migration completed",
            extra={"event": "migration_completed", "metadata": metadata},
        )

    resulting_history = applied_migrations(connection)
    _validate_history(resulting_history, migrations)
    return current_schema_version(connection)
