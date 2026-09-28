# ruff: noqa: E501
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from syntra_build.infrastructure.backup import (
    BackupError,
    BackupReason,
    SQLiteBackupService,
    restore_verify,
)
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.config.models import ApplicationConfig
from syntra_build.infrastructure.persistence import bootstrap_database


def config(tmp_path: Path) -> ApplicationConfig:
    data = tmp_path / "data"
    return load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "app"),
                "configuration_root": str(tmp_path / "etc"),
                "data_root": str(data),
                "log_root": str(tmp_path / "log"),
                "workspace_root": str(data / "workspaces"),
                "backup_root": str(data / "backups"),
            },
            "database": {"sqlite_path": str(data / "syntra.db")},
        },
        environ={},
    )


def test_online_wal_backup_restore_and_retention(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.filesystem.data_root.mkdir()
    connection = bootstrap_database(cfg)
    connection.execute(
        "INSERT INTO projects(id,name,state,activity,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "p",
            "Project",
            "NEW",
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "project",
            "public",
        ),
    )
    service = SQLiteBackupService(cfg.database.sqlite_path, cfg.filesystem.backup_root)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    old = service.create(BackupReason.AUTOMATIC, now=now - timedelta(days=31))
    current = service.create(now=now)
    assert restore_verify(current.path) == 25
    assert service.automatic_due(now + timedelta(hours=23)) is False
    assert service.retain(30, now=now, keep=current.path) == (old.path,)
    assert current.path.exists()
    (cfg.filesystem.backup_root / "unrelated.txt").write_text("keep")
    service.retain(1, now=now + timedelta(days=2), keep=current.path)
    assert (cfg.filesystem.backup_root / "unrelated.txt").exists()
    connection.close()


def test_collision_and_corruption(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.filesystem.data_root.mkdir()
    bootstrap_database(cfg).close()
    service = SQLiteBackupService(cfg.database.sqlite_path, cfg.filesystem.backup_root)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    first, second = service.create(now=now), service.create(now=now)
    assert first.path != second.path
    corrupt = tmp_path / "bad.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(BackupError):
        restore_verify(corrupt)
