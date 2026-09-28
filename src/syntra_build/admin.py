# ruff: noqa: E501
"""Stable, deliberately narrow operator CLI."""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from syntra_build.application.operational_health import (
    LocalResourceSampler,
    OperationalHealth,
)
from syntra_build.application.status import SQLiteStatusService
from syntra_build.infrastructure.backup import (
    BackupReason,
    SQLiteBackupService,
    restore_verify,
    verify_database,
)
from syntra_build.infrastructure.config.host import DEFAULT_HOST_CONFIG_PATH
from syntra_build.infrastructure.config.loader import load_config
from syntra_build.infrastructure.config.models import ApplicationConfig
from syntra_build.infrastructure.persistence import bootstrap_database
from syntra_build.infrastructure.persistence.migrations import current_schema_version
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository


def _config(path: Path) -> ApplicationConfig:
    raw = json.loads(path.read_text())
    # Local/read-only administration intentionally does not load provider secrets.
    for section in ("telegram", "architect", "github"):
        if isinstance(raw.get(section), dict):
            raw[section]["enabled"] = False
    return load_config(raw, environ={})


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="syntra-build-admin")
    result.add_argument("--config", type=Path, default=DEFAULT_HOST_CONFIG_PATH)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("version")
    commands.add_parser("health")
    projects = commands.add_parser("projects")
    group = projects.add_mutually_exclusive_group()
    group.add_argument("--active", action="store_true")
    group.add_argument("--waiting", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("project")
    commands.add_parser("integrity")
    backup = commands.add_parser("backup")
    backup.add_argument(
        "--reason", choices=[r.value for r in BackupReason], default="manual"
    )
    restore = commands.add_parser("restore-verify")
    restore.add_argument("backup", type=Path)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--apply", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = _config(args.config)
        if args.command == "restore-verify":
            print(f"PASS schema={restore_verify(args.backup)}")
            return 0
        connection = bootstrap_database(config)
        try:
            if args.command == "version":
                revision_file = config.filesystem.application_root / "REVISION"
                revision = (
                    revision_file.read_text().strip()
                    if revision_file.is_file()
                    else "unknown"
                )
                print(
                    f"version={version('syntra-build')} revision={revision} schema={current_schema_version(connection)} python={platform.python_version()}"
                )
            elif args.command == "integrity":
                print(
                    f"PASS schema={verify_database(config.database.sqlite_path, full=True)}"
                )
            elif args.command == "backup":
                backup_service = SQLiteBackupService(
                    config.database.sqlite_path, config.filesystem.backup_root
                )
                backup = backup_service.create(BackupReason(args.reason))
                backup_service.retain(
                    config.backups.retention_days,
                    now=backup.created_at,
                    keep=backup.path,
                )
                print(
                    f"path={backup.path} schema={backup.schema_version} bytes={backup.byte_size} integrity=ok"
                )
            elif args.command == "health":
                health = OperationalHealth(
                    LocalResourceSampler(config.filesystem.data_root),
                    config.security,
                    lambda: _database_ok(connection),
                )
                health.running()
                projection = health.projection()
                print(
                    f"state={projection.state.value} ready={str(projection.ready).lower()} reasons={','.join(r.value for r in projection.reasons) or 'none'} disk_free_percent={projection.resources.free_percent:.2f} artifact_bytes={projection.resources.artifact_bytes} database_available={str(_database_ok(connection)).lower()}"
                )
                return 0 if projection.ready else 1
            elif args.command in {"projects", "status"}:
                status_service = SQLiteStatusService(
                    connection,
                    SQLiteProjectRepository(connection, lambda: str(uuid4())),
                )
                if args.command == "status":
                    resolved = status_service.resolve_project(args.project)
                    if resolved.project is None:
                        print("project not found", file=sys.stderr)
                        return 2
                    project_status = status_service.project_status(resolved.project.id)
                    print(
                        f"{project_status.name}\t{project_status.state.value}\t{project_status.activity}\t{project_status.next_action}"
                    )
                elif args.waiting:
                    for action in status_service.waiting_for_human():
                        print(f"{action.project_name}\tWAITING_HUMAN\t{action.title}")
                else:
                    items = (
                        status_service.active_projects()
                        if args.active
                        else tuple(
                            status_service.project_status(p.id)
                            for p in status_service.list_projects()
                        )
                    )
                    for project_status in items:
                        print(
                            f"{project_status.name}\t{project_status.state.value}\t{project_status.activity}"
                        )
            elif args.command == "reconcile":
                count = connection.execute(
                    "SELECT count(*) FROM projects WHERE state NOT IN ('COMPLETE','FAILED','CANCELLED')"
                ).fetchone()[0]
                print(f"Recovery observations: incomplete_projects={count}")
                print(
                    "Proposed action: run trusted M27 startup recovery; mutation_required="
                    + ("yes" if count else "no")
                )
                if args.apply:
                    print(
                        "Apply unavailable without configured trusted provider composition; no mutation performed",
                        file=sys.stderr,
                    )
                    return 3
            return 0
        finally:
            connection.close()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def _database_ok(connection: sqlite3.Connection) -> bool:
    return connection.execute("SELECT 1").fetchone() is not None


if __name__ == "__main__":
    raise SystemExit(main())
