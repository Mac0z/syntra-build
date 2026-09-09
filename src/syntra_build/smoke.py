"""Bounded M6A development-deployment smoke commands."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO
from uuid import uuid4

from syntra_build.adapters.github import (
    GitHubRepositoryNameConflictChecker,
    GitHubTransport,
)
from syntra_build.adapters.telegram import TelegramClient, TelegramInboundMessage
from syntra_build.adapters.telegram.application import route_authorized_message
from syntra_build.application.commands import CommandRouter
from syntra_build.application.commands.models import Command, InboundMessage
from syntra_build.application.commands.services import (
    CommandAuditRequest,
    ProjectCommandResult,
    ProjectResolution,
    ProjectSummary,
    ResolutionOutcome,
)
from syntra_build.application.projects import (
    ProjectCreationService,
    SQLiteProjectQueryService,
)
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)
from syntra_build.infrastructure.logging import configure_logging
from syntra_build.infrastructure.persistence import (
    SQLiteProjectRepository,
    SQLiteWorkflowEventRepository,
    bootstrap_database,
)

_LOGGER = logging.getLogger("syntra_build.smoke")
_REVISION = re.compile(r"[0-9a-f]{40}")
_SUCCESS_HEALTH = "development runtime available (local initialization passed)"


class TelegramSmokeClient(Protocol):
    """The existing M5 operations needed by one smoke cycle."""

    def poll_updates(
        self, *, offset: int | None = None
    ) -> tuple[TelegramInboundMessage, ...]: ...

    def send_text(
        self,
        *,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class LocalSmokeResult:
    revision: str
    python_version: str
    database: str
    routing: str


class _UnavailableProjectQueries:
    def list_projects(self) -> tuple[ProjectSummary, ...]:
        return ()

    def resolve_project(self, reference: str) -> ProjectResolution:
        return ProjectResolution(ResolutionOutcome.NOT_FOUND)

    def get_project_status(self, project_id: ProjectId) -> ProjectSummary:
        raise RuntimeError("project services are unavailable in M6A smoke mode")


class _UnavailableProjectCommands:
    def handle(self, command: Command, project: ProjectSummary) -> ProjectCommandResult:
        return ProjectCommandResult(
            "Project mutations are unavailable in M6A smoke mode.", performed=False
        )


class _NoopAudit:
    def record(self, request: CommandAuditRequest) -> None:
        return None


class _LocalHealth:
    def current_health(self) -> str:
        return _SUCCESS_HEALTH


def build_smoke_router() -> CommandRouter:
    """Compose M6 with explicit, truthful pre-M7 service implementations."""
    return CommandRouter(
        project_queries=_UnavailableProjectQueries(),
        project_commands=_UnavailableProjectCommands(),
        audit_sink=_NoopAudit(),
        health=_LocalHealth(),
    )


def build_host_router(
    config: ApplicationConfig,
    connection: sqlite3.Connection,
    *,
    github_transport: GitHubTransport | None = None,
) -> CommandRouter:
    """Compose the durable M14 services used by real Telegram host routing."""
    projects = SQLiteProjectRepository(connection, lambda: str(uuid4()))
    events = SQLiteWorkflowEventRepository(connection)
    checker = (
        GitHubRepositoryNameConflictChecker(config, transport=github_transport)
        if github_transport is not None
        else GitHubRepositoryNameConflictChecker(config)
    )
    creation = ProjectCreationService(
        connection,
        projects,
        events,
        checker,
    )
    return CommandRouter(
        project_queries=SQLiteProjectQueryService(projects),
        project_commands=_UnavailableProjectCommands(),
        audit_sink=_NoopAudit(),
        health=_LocalHealth(),
        project_creation=creation,
    )


def validate_filesystem(config: ApplicationConfig) -> None:
    """Require provisioned host roots and writable runtime-owned locations."""
    for path in (
        config.filesystem.application_root,
        config.filesystem.configuration_root,
        config.filesystem.data_root,
        config.filesystem.log_root,
    ):
        if not path.is_dir():
            raise RuntimeError("a configured filesystem root is unavailable")
    for path in (config.filesystem.data_root, config.filesystem.log_root):
        if not os.access(path, os.W_OK):
            raise RuntimeError("a configured runtime directory is not writable")


def run_local_smoke(
    config: ApplicationConfig,
    revision: str,
    *,
    bootstrap: Callable[[ApplicationConfig], sqlite3.Connection] = bootstrap_database,
) -> LocalSmokeResult:
    """Validate local initialization and exact ping routing without Telegram."""
    validate_revision(revision)
    validate_filesystem(config)
    _LOGGER.info(
        "Host smoke started",
        extra={"event": "host_smoke_started", "metadata": {"revision": revision}},
    )
    connection = bootstrap(config)
    try:
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if foreign_keys != 1 or str(journal_mode).casefold() != "wal":
            raise RuntimeError("database safety settings are not active")
    finally:
        connection.close()

    response = build_smoke_router().route(
        InboundMessage(
            source_platform="local-smoke",
            source_update_id="local-1",
            source_message_id="local-1",
            sender_id="operator",
            received_at=datetime.now(UTC),
            text="ping",
        )
    )
    if response.text != "pong":
        raise RuntimeError("deterministic ping routing failed")
    _LOGGER.info(
        "Host smoke completed",
        extra={
            "event": "host_smoke_completed",
            "metadata": {"revision": revision, "database": "ok", "routing": "pong"},
        },
    )
    version = ".".join(str(item) for item in sys.version_info[:3])
    return LocalSmokeResult(revision, version, "OK", response.text)


def run_telegram_once(client: TelegramSmokeClient, router: CommandRouter) -> int:
    """Perform exactly one M5 poll, then route/send authorized updates in order."""
    _LOGGER.info("Telegram smoke started", extra={"event": "telegram_smoke_started"})
    messages = client.poll_updates()
    for message in messages:
        response = route_authorized_message(message, router)
        client.send_text(
            chat_id=message.chat_id,
            text=response.text,
            thread_id=message.thread_id,
            reply_to_message_id=message.message_id,
        )
    _LOGGER.info(
        "Telegram smoke completed",
        extra={
            "event": "telegram_smoke_completed",
            "metadata": {"processed_count": len(messages)},
        },
    )
    return len(messages)


def validate_revision(value: str) -> str:
    revision = value.strip().lower()
    if _REVISION.fullmatch(revision) is None:
        raise RuntimeError("deployed revision must be an exact 40-character Git SHA")
    return revision


def _read_secret_file(path: Path, label: str) -> SecretValue | None:
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"{label} file permissions are too broad")
    return SecretValue(path.read_text(encoding="utf-8").strip())


def _load_host_config(
    config_path: Path, token_path: Path, github_token_path: Path
) -> ApplicationConfig:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise RuntimeError("configuration file must contain a JSON object")
    return load_config(
        raw,
        environ={},
        secrets=SecretInputs(
            telegram_bot_token=_read_secret_file(token_path, "Telegram token"),
            github_token=_read_secret_file(github_token_path, "GitHub token"),
        ),
    )


def _configure_file_logging(config: ApplicationConfig) -> TextIO:
    stream = (config.filesystem.log_root / "smoke.log").open("a", encoding="utf-8")
    configure_logging(config, stream=stream)
    return stream


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Syntra Build M6A smoke runner")
    parser.add_argument("command", choices=("local", "telegram-once"))
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/syntra-build/config.json")
    )
    parser.add_argument(
        "--token-file", type=Path, default=Path("/etc/syntra-build/telegram-token")
    )
    parser.add_argument(
        "--github-token-file",
        type=Path,
        default=Path("/etc/syntra-build/github-token"),
    )
    parser.add_argument(
        "--revision-file", type=Path, default=Path("/opt/syntra-build/REVISION")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    stream: TextIO | None = None
    try:
        if sys.version_info[:2] != (3, 14):
            raise RuntimeError("Python 3.14 is required")
        config = _load_host_config(args.config, args.token_file, args.github_token_file)
        stream = _configure_file_logging(config)
        revision = validate_revision(args.revision_file.read_text(encoding="ascii"))
        if args.command == "local":
            result = run_local_smoke(config, revision)
            print("Syntra Build development smoke: PASS")
            print(f"Revision: {result.revision}")
            print(f"Python: {result.python_version}")
            print(f"Database: {result.database}")
            print(f"Routing: {result.routing}")
        else:
            validate_filesystem(config)
            connection = bootstrap_database(config)
            try:
                count = run_telegram_once(
                    TelegramClient(config), build_host_router(config, connection)
                )
            finally:
                connection.close()
            print(f"Syntra Build Telegram smoke: PASS ({count} authorised updates)")
        return 0
    except Exception:
        if stream is not None:
            _LOGGER.exception(
                "Smoke command failed", extra={"event": "host_smoke_failed"}
            )
        print("Syntra Build development smoke: FAIL", file=sys.stderr)
        return 1
    finally:
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
