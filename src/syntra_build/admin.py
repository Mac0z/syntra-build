# ruff: noqa: E501
"""Stable, deliberately narrow operator CLI."""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from syntra_build import __version__
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
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    applied_migrations,
    current_schema_version,
)
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.recovery import SQLiteRecoveryRepository


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
    commands.add_parser("reconcile")
    recovery = commands.add_parser("recover-empty-implementation")
    recovery.add_argument("project_id")
    recovery.add_argument("milestone_id")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = _config(args.config)
        if args.command == "restore-verify":
            print(f"PASS schema={restore_verify(args.backup)}")
            return 0
        if args.command == "backup":
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
            return 0
        if args.command == "recover-empty-implementation":
            from syntra_build.application.implementation_recovery import (
                recover_empty_implementation,
            )
            from syntra_build.domain import MilestoneId, ProjectId
            from syntra_build.infrastructure.git_workspace import TrustedGit
            from syntra_build.infrastructure.persistence.connection import open_database

            # Require schema compatibility before opening for the explicitly requested mutation.
            _open_observation_database(config.database.sqlite_path).close()
            connection = open_database(config.database.sqlite_path)
            try:
                recovered_job = recover_empty_implementation(
                    connection,
                    TrustedGit(config.filesystem.data_root / "git-auth"),
                    config.filesystem.data_root,
                    ProjectId.from_string(args.project_id),
                    MilestoneId.from_string(args.milestone_id),
                    cycle_limit=config.retries.codex_cycle_limit,
                )
                print(
                    f"job={recovered_job} project_remains_paused=true provider_invoked=false"
                )
                return 0
            finally:
                connection.close()
        connection = _open_observation_database(config.database.sqlite_path)
        try:
            if args.command == "version":
                revision_file = config.filesystem.application_root / "REVISION"
                revision = (
                    revision_file.read_text().strip()
                    if revision_file.is_file()
                    else "unknown"
                )
                print(
                    f"version={_package_version()} revision={revision} schema={current_schema_version(connection)} python={platform.python_version()}"
                )
            elif args.command == "integrity":
                print(
                    f"PASS schema={verify_database(config.database.sqlite_path, full=True)}"
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
                subjects = SQLiteRecoveryRepository(
                    connection, lambda: str(uuid4())
                ).discover()
                print(f"Recovery observations: subjects={len(subjects)}")
                for subject in subjects:
                    state = subject.milestone_state or subject.project_state
                    uncertain = state in {
                        "COMMITTING",
                        "PUSHING",
                        "PR_CREATING",
                        "MERGING",
                        "MERGE_VERIFY",
                    }
                    print(
                        f"project={subject.project_id} milestone={subject.milestone_id or '-'} "
                        f"milestone_state={subject.milestone_state or '-'} job={subject.job_id or '-'} "
                        f"job_state={subject.job_state or '-'} category={state} "
                        f"uncertain_external_side_effect={str(uncertain).lower()} "
                        "required_path=M27_RECOVERY"
                    )
            return 0
        finally:
            connection.close()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def _database_ok(connection: sqlite3.Connection) -> bool:
    return connection.execute("SELECT 1").fetchone() is not None


def _package_version() -> str:
    try:
        return version("syntra-build")
    except PackageNotFoundError:
        return __version__


def _open_observation_database(path: Path) -> sqlite3.Connection:
    """Open without WAL/configuration writes and require exact code/schema identity."""
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    history = applied_migrations(connection)
    expected = tuple((item.version, item.name) for item in MIGRATIONS)
    if history != expected:
        connection.close()
        relation = "behind" if len(history) < len(expected) else "ahead or inconsistent"
        raise RuntimeError(
            f"database schema is {relation}; service maintenance required"
        )
    return connection


if __name__ == "__main__":
    raise SystemExit(main())
