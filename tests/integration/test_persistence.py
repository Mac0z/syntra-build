from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from syntra_build.infrastructure.config import ApplicationConfig, load_config
from syntra_build.infrastructure.persistence import (
    BUSY_TIMEOUT_MILLISECONDS,
    MIGRATIONS,
    DatabaseIntegrityError,
    Migration,
    MigrationError,
    TransactionError,
    apply_migrations,
    bootstrap_database,
    check_database_integrity,
    current_schema_version,
    open_database,
    transaction,
    validate_integrity_results,
    validate_migrations,
)


def config(root: Path) -> ApplicationConfig:
    (root / "data").mkdir()
    return load_config(
        {
            "filesystem": {
                "application_root": root / "app",
                "configuration_root": root / "config",
                "data_root": root / "data",
                "log_root": root / "logs",
            },
            "database": {"sqlite_path": root / "data" / "syntra.db"},
        },
        environ={},
    )


def test_bootstrap_creation_and_idempotency(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    with bootstrap_database(cfg) as db:
        assert cfg.database.sqlite_path.exists() and current_schema_version(db) == len(
            MIGRATIONS
        )
        assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[
            0
        ] == len(MIGRATIONS)
    with bootstrap_database(cfg) as db:
        assert current_schema_version(db) == len(MIGRATIONS)
        assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[
            0
        ] == len(MIGRATIONS)


def test_connection_configuration_is_consistent(tmp_path: Path) -> None:
    path = tmp_path / "settings.db"
    for _ in range(2):
        with open_database(path) as db:
            assert db.row_factory is sqlite3.Row
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert db.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
            assert (
                db.execute("PRAGMA busy_timeout").fetchone()[0]
                == BUSY_TIMEOUT_MILLISECONDS
            )


def test_migration_order_validation() -> None:
    next_version = len(MIGRATIONS) + 1
    next_migration = Migration(
        next_version,
        f"{next_version:03}_next",
        ("CREATE TABLE next_table (id INTEGER)",),
    )
    validate_migrations((*MIGRATIONS, next_migration))
    for invalid in (
        (next_migration, *MIGRATIONS),
        (MIGRATIONS[0], MIGRATIONS[0]),
    ):
        with pytest.raises(MigrationError, match="unique, contiguous, and ordered"):
            validate_migrations(invalid)


def test_failed_migration_is_atomic(tmp_path: Path) -> None:
    migrations = (
        *MIGRATIONS,
        Migration(
            len(MIGRATIONS) + 1,
            "next_fails",
            ("CREATE TABLE partial (id INTEGER)", "INSERT INTO missing VALUES (1)"),
        ),
    )
    with open_database(tmp_path / "failure.db") as db:
        with pytest.raises(MigrationError, match=f"migration {len(MIGRATIONS) + 1}"):
            apply_migrations(db, migrations)
        assert current_schema_version(db) == len(MIGRATIONS)
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='partial'"
            ).fetchone()[0]
            == 0
        )


def test_unsupported_histories_are_rejected(tmp_path: Path) -> None:
    with open_database(tmp_path / "newer.db") as db:
        apply_migrations(db)
        unknown_version = len(MIGRATIONS) + 1
        db.execute(
            "INSERT INTO schema_migrations VALUES (?,?)",
            (unknown_version, "unknown"),
        )
        with pytest.raises(MigrationError, match="newer"):
            apply_migrations(db)
    with open_database(tmp_path / "wrong.db") as db:
        apply_migrations(db)
        db.execute("UPDATE schema_migrations SET name='wrong' WHERE version=1")
        with pytest.raises(MigrationError, match="unknown or inconsistent"):
            apply_migrations(db)


def test_integrity_validation(tmp_path: Path) -> None:
    with open_database(tmp_path / "healthy.db") as db:
        check_database_integrity(db)
    with pytest.raises(DatabaseIntegrityError, match="integrity"):
        validate_integrity_results(["problem"])


def test_transaction_commit_rollback_and_nesting(tmp_path: Path) -> None:
    with open_database(tmp_path / "tx.db") as db:
        db.execute("CREATE TABLE sample (value TEXT)")
        with transaction(db):
            db.execute("INSERT INTO sample VALUES ('kept')")
        with pytest.raises(ValueError):
            with transaction(db):
                db.execute("INSERT INTO sample VALUES ('lost')")
                raise ValueError
        assert [row[0] for row in db.execute("SELECT value FROM sample")] == ["kept"]
        with transaction(db):
            with pytest.raises(TransactionError, match="nested"):
                with transaction(db):
                    pass


def test_bootstrap_logs_lifecycle_without_sql(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="syntra_build")
    with bootstrap_database(config(tmp_path)):
        pass
    events = {getattr(record, "event", None) for record in caplog.records}
    assert {
        "database_opened",
        "migration_started",
        "migration_completed",
        "database_integrity_checked",
    } <= events
    assert "CREATE TABLE" not in caplog.text
