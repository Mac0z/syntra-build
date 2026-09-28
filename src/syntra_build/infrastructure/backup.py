# ruff: noqa: E501
"""Verified, online SQLite backups and conservative retention."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Lock

from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    applied_migrations,
    current_schema_version,
)

_NAME = re.compile(r"^syntra-(\d{8}T\d{6}Z)-schema(\d+)-([a-z-]+)(?:-(\d+))?\.sqlite3$")


class BackupReason(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    PRE_MIGRATION = "pre-migration"
    PRE_UPGRADE = "pre-upgrade"


@dataclass(frozen=True, slots=True)
class BackupMetadata:
    path: Path
    created_at: datetime
    schema_version: int
    byte_size: int
    integrity_result: str = "ok"


class BackupError(RuntimeError):
    pass


def verify_database(path: Path, *, full: bool = False) -> int:
    """Independently validate SQLite integrity, FKs, and exact migration history."""
    if path.is_symlink() or not path.is_file() or not os.access(path, os.R_OK):
        raise BackupError("backup must be a readable regular non-symlink file")
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            check = "integrity_check" if full else "quick_check"
            if [
                str(r[0]).casefold() for r in connection.execute(f"PRAGMA {check}")
            ] != ["ok"]:
                raise BackupError("database integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("database foreign-key check failed")
            history = applied_migrations(connection)
            expected = tuple((m.version, m.name) for m in MIGRATIONS[: len(history)])
            if history != expected or not history:
                raise BackupError("database migration history is unsupported")
            return current_schema_version(connection)
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupError("database could not be verified") from error


def restore_verify(path: Path) -> int:
    """Prove that an operator-selected backup restores in a private clean location."""
    if path.is_symlink() or not path.is_file():
        raise BackupError("backup must be a regular non-symlink file")
    with tempfile.TemporaryDirectory(prefix="syntra-restore-") as directory:
        restored = Path(directory) / "restored.sqlite3"
        shutil.copyfile(path, restored, follow_symlinks=False)
        return verify_database(restored, full=True)


class SQLiteBackupService:
    def __init__(self, source: Path, root: Path) -> None:
        self.source, self.root = source.resolve(), root.resolve()
        self._lock = Lock()

    def create(
        self, reason: BackupReason = BackupReason.MANUAL, *, now: datetime | None = None
    ) -> BackupMetadata:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if not self._lock.acquire(blocking=False):
            raise BackupError("backup already running")
        partial: Path | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            version = self._source_version()
            stem = f"syntra-{now:%Y%m%dT%H%M%SZ}-schema{version}-{reason.value}"
            destination = self.root / f"{stem}.sqlite3"
            sequence = 1
            while destination.exists():
                destination = self.root / f"{stem}-{sequence}.sqlite3"
                sequence += 1
            partial = self.root / f".{destination.name}.partial"
            source = sqlite3.connect(f"file:{self.source.as_posix()}?mode=ro", uri=True)
            target = sqlite3.connect(partial)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            verified = verify_database(partial)
            if verified != version:
                raise BackupError("backup schema differs from source")
            os.replace(partial, destination)
            return BackupMetadata(destination, now, version, destination.stat().st_size)
        except (OSError, sqlite3.Error) as error:
            raise BackupError("online backup failed") from error
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)
            self._lock.release()

    def _source_version(self) -> int:
        connection = sqlite3.connect(f"file:{self.source.as_posix()}?mode=ro", uri=True)
        try:
            return current_schema_version(connection)
        finally:
            connection.close()

    def latest(self) -> BackupMetadata | None:
        candidates: list[BackupMetadata] = []
        if not self.root.is_dir():
            return None
        for path in self.root.iterdir():
            match = _NAME.fullmatch(path.name)
            if match and path.is_file() and not path.is_symlink():
                created = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=UTC
                )
                candidates.append(
                    BackupMetadata(path, created, int(match[2]), path.stat().st_size)
                )
        return max(
            candidates, key=lambda item: (item.created_at, item.path.name), default=None
        )

    def automatic_due(self, now: datetime) -> bool:
        latest = self.latest()
        return latest is None or now.astimezone(UTC) - latest.created_at >= timedelta(
            hours=24
        )

    def retain(
        self, days: int, *, now: datetime, keep: Path | None = None
    ) -> tuple[Path, ...]:
        """Delete recognised files strictly older than the UTC cutoff; never symlinks."""
        cutoff = now.astimezone(UTC) - timedelta(days=days)
        removed: list[Path] = []
        if not self.root.is_dir():
            return ()
        for path in self.root.iterdir():
            match = _NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file() or path == keep:
                continue
            created = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            if created < cutoff:
                path.unlink()
                removed.append(path)
        return tuple(sorted(removed))
