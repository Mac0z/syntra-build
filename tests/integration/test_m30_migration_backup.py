from pathlib import Path

import pytest

from syntra_build.infrastructure.backup import (
    BackupError,
    SQLiteBackupService,
    verify_database,
)
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.config.models import ApplicationConfig
from syntra_build.infrastructure.persistence import bootstrap_database
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.errors import MigrationError
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
)


def _config(tmp_path: Path) -> ApplicationConfig:
    data = tmp_path / "data"
    data.mkdir()
    return load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "app"),
                "configuration_root": str(tmp_path / "etc"),
                "data_root": str(data),
                "log_root": str(tmp_path / "log"),
                "workspace_root": str(data / "work"),
                "backup_root": str(data / "backups"),
            },
            "database": {"sqlite_path": str(data / "syntra.db")},
        },
        environ={},
    )


def test_fresh_and_current_database_do_not_create_migration_backup(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    bootstrap_database(config).close()
    assert not config.filesystem.backup_root.exists()
    bootstrap_database(config).close()
    assert not config.filesystem.backup_root.exists()


def test_old_schema_is_verified_before_migration(tmp_path: Path) -> None:
    config = _config(tmp_path)
    connection = open_database(config.database.sqlite_path)
    apply_migrations(connection, MIGRATIONS[:-1])
    connection.close()
    migrated = bootstrap_database(config)
    assert current_schema_version(migrated) == 25
    migrated.close()
    backups = tuple(config.filesystem.backup_root.glob("*-pre-migration.sqlite3"))
    assert len(backups) == 1
    assert verify_database(backups[0]) == 24


def test_failed_backup_prevents_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    connection = open_database(config.database.sqlite_path)
    apply_migrations(connection, MIGRATIONS[:-1])
    connection.close()

    def fail(*_args: object, **_kwargs: object) -> object:
        raise BackupError("injected")

    monkeypatch.setattr(SQLiteBackupService, "create", fail)
    with pytest.raises(BackupError):
        bootstrap_database(config)
    connection = open_database(config.database.sqlite_path)
    assert current_schema_version(connection) == 24
    connection.close()


def test_schema_ahead_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    connection = bootstrap_database(config)
    connection.execute("INSERT INTO schema_migrations VALUES (26, 'unknown')")
    connection.close()
    with pytest.raises(MigrationError):
        bootstrap_database(config)
