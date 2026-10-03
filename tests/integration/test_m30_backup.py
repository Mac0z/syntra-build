# ruff: noqa: E501
import fcntl
import multiprocessing
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest

import syntra_build.infrastructure.backup as backup_module
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
    current = service.create(BackupReason.AUTOMATIC, now=now)
    assert restore_verify(current.path) == 28
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


def test_reason_filtered_automatic_due(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.filesystem.data_root.mkdir()
    bootstrap_database(cfg).close()
    service = SQLiteBackupService(cfg.database.sqlite_path, cfg.filesystem.backup_root)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    service.create(BackupReason.AUTOMATIC, now=now - timedelta(hours=25))
    service.create(BackupReason.MANUAL, now=now)
    service.create(BackupReason.PRE_UPGRADE, now=now)
    assert service.automatic_due(now)
    service.create(BackupReason.AUTOMATIC, now=now)
    assert not service.automatic_due(now + timedelta(hours=23, minutes=59))
    assert service.automatic_due(now + timedelta(hours=24))


class _EventLike(Protocol):
    def set(self) -> None: ...
    def wait(self, timeout: float | None = None) -> bool: ...


def _hold_backup_lock(root: str, ready: _EventLike, release: _EventLike) -> None:
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path / ".backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    ready.set()
    release.wait(5)
    os.close(descriptor)


def test_interprocess_lock_serializes_distinct_services(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.filesystem.data_root.mkdir()
    bootstrap_database(cfg).close()
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    process = context.Process(
        target=_hold_backup_lock,
        args=(str(cfg.filesystem.backup_root), ready, release),
    )
    process.start()
    assert ready.wait(2)
    try:
        with pytest.raises(BackupError, match="already running"):
            SQLiteBackupService(
                cfg.database.sqlite_path, cfg.filesystem.backup_root
            ).create()
        assert not tuple(cfg.filesystem.backup_root.glob("*.sqlite3"))
    finally:
        release.set()
        process.join(2)
    assert process.exitcode == 0


def test_verification_failure_cleans_unique_partial_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    cfg.filesystem.data_root.mkdir()
    bootstrap_database(cfg).close()

    def fail(_path: Path, *, full: bool = False) -> int:
        del full
        raise BackupError("injected verification failure")

    monkeypatch.setattr(backup_module, "verify_database", fail)
    with pytest.raises(BackupError, match="verification failure"):
        SQLiteBackupService(
            cfg.database.sqlite_path, cfg.filesystem.backup_root
        ).create()
    assert not tuple(cfg.filesystem.backup_root.glob("*.sqlite3"))
    assert not tuple(cfg.filesystem.backup_root.glob("*.partial"))


def test_wal_commit_visibility_uncommitted_exclusion_and_symlink_retention(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    cfg.filesystem.data_root.mkdir()
    writer = bootstrap_database(cfg)
    writer.execute(
        "INSERT INTO projects(id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES(?,?,?,?,?,?,?,?)",
        (
            "committed",
            "Committed",
            "NEW",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "committed",
            "public",
        ),
    )
    writer.execute("BEGIN")
    writer.execute(
        "INSERT INTO projects(id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES(?,?,?,?,?,?,?,?)",
        (
            "uncommitted",
            "Uncommitted",
            "NEW",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "uncommitted",
            "public",
        ),
    )
    writer.rollback()
    service = SQLiteBackupService(cfg.database.sqlite_path, cfg.filesystem.backup_root)
    backup = service.create(now=datetime(2026, 9, 28, tzinfo=UTC))
    with sqlite3.connect(backup.path) as restored:
        assert restored.execute("SELECT id FROM projects ORDER BY id").fetchall() == [
            ("committed",)
        ]
    writer.close()
    unrelated = cfg.filesystem.backup_root / "unrelated"
    unrelated.write_text("safe")
    link = (
        cfg.filesystem.backup_root / "syntra-20200101T000000Z-schema25-manual.sqlite3"
    )
    link.symlink_to(unrelated)
    service.retain(1, now=datetime(2026, 9, 28, tzinfo=UTC), keep=backup.path)
    assert unrelated.exists() and link.is_symlink()


def test_invalid_history_and_foreign_key_are_rejected(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.filesystem.data_root.mkdir()
    bootstrap_database(cfg).close()
    service = SQLiteBackupService(cfg.database.sqlite_path, cfg.filesystem.backup_root)
    invalid_history = service.create().path
    with sqlite3.connect(invalid_history) as connection:
        connection.execute(
            "UPDATE schema_migrations SET name='tampered' WHERE version=25"
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with pytest.raises(BackupError, match="migration history"):
        restore_verify(invalid_history)
    invalid_fk = service.create().path
    with sqlite3.connect(invalid_fk) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO milestone_dependencies VALUES ('missing-a','missing-b')"
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with pytest.raises(BackupError, match="foreign-key"):
        restore_verify(invalid_fk)
