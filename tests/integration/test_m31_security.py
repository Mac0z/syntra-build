import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from syntra_build.application.security import SecurityPolicy, UnsafeSecurityDetails
from syntra_build.domain import Project, ProjectId, ProjectState
from syntra_build.domain.security import SecurityEventType, SecuritySeverity
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.persistence import (
    bootstrap_database,
    current_schema_version,
)
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _db(tmp_path: Path) -> sqlite3.Connection:
    config = load_config(
        {
            "filesystem": {
                "data_root": str(tmp_path),
                "workspace_root": str(tmp_path / "workspaces"),
                "repository_root": str(tmp_path / "repositories"),
                "artifact_root": str(tmp_path / "artifacts"),
                "backup_root": str(tmp_path / "backups"),
            },
            "database": {"sqlite_path": str(tmp_path / "db.sqlite")},
        }
    )
    return bootstrap_database(config)


def test_migration_26_and_active_resolution_history(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert current_schema_version(db) == 26
    project = Project(
        ProjectId.from_string("00000000-0000-4000-8000-000000000001"),
        "project",
        ProjectState.NEW,
        NOW,
        NOW,
    )
    SQLiteProjectRepository(db, lambda: "transition").add(project)
    ids = iter(("event", "resolution"))
    policy = SecurityPolicy(db, lambda: next(ids))
    event = policy.record(
        SecurityEventType.SECRET_DETECTED,
        SecuritySeverity.HIGH,
        project_id=project.id,
        source_component="change_validation",
        correlation_id="corr",
        safe_details={"rule_id": "GITHUB_TOKEN"},
        blocking=True,
        created_at=NOW,
    )
    assert policy.blocked(project.id)
    policy.resolve(event.id, "CLEAN_REVALIDATION", "corr-2", "SYSTEM", resolved_at=NOW)
    assert not policy.blocked(project.id)
    assert db.execute("SELECT count(*) FROM security_events").fetchone()[0] == 1
    with pytest.raises(Exception):
        db.execute("DELETE FROM security_events WHERE id='event'")


def test_security_details_fail_closed_without_persisting_secret(tmp_path: Path) -> None:
    db = _db(tmp_path)
    policy = SecurityPolicy(db, lambda: "event")
    with pytest.raises(UnsafeSecurityDetails):
        policy.record(
            SecurityEventType.SECRET_DETECTED,
            SecuritySeverity.HIGH,
            source_component="test",
            correlation_id="corr",
            safe_details={"token": "synthetic"},
            blocking=True,
        )
    assert db.execute("SELECT count(*) FROM security_events").fetchone()[0] == 0
