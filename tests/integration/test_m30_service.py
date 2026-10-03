from __future__ import annotations

import json
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs
from urllib.request import urlopen

import pytest

from syntra_build.adapters.telegram.client import HTTPResponse, TelegramClient
from syntra_build.domain import (
    Milestone,
    MilestoneId,
    MilestoneState,
    Project,
    ProjectId,
    ProjectState,
)
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.config.models import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
)
from syntra_build.infrastructure.persistence import bootstrap_database
from syntra_build.infrastructure.persistence.milestones import SQLiteMilestoneRepository
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.service import ServiceRuntime


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(
    tmp_path: Path,
    *,
    metrics: bool = True,
    telegram: bool = False,
    github: bool = False,
) -> ApplicationConfig:
    data = tmp_path / "data"
    for path in (data, tmp_path / "app", tmp_path / "etc", tmp_path / "log"):
        path.mkdir(parents=True, exist_ok=True)
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
            "metrics": {"enabled": metrics, "port": _port()},
            "backups": {"enabled": False, "retention_days": 30},
            "telegram": {
                "enabled": telegram,
                "authorised_user_ids": [123] if telegram else [],
                "polling_timeout_seconds": 0.01,
            },
            "github": {"enabled": github, "owner": "example" if github else None},
        },
        environ={},
        secrets=SecretInputs(
            telegram_bot_token=(
                SecretValue("synthetic-test-token") if telegram else None
            ),
            github_token=SecretValue("synthetic-github-token") if github else None,
        ),
    )


def test_real_runtime_thread_and_http_health(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runtime = ServiceRuntime(
        tmp_path / "config.json",
        config_loader=lambda _path: config,
        loop_interval=0.01,
        shutdown_grace=1,
        configure_runtime_logging=False,
        executor_factory=lambda _config: {},
    )
    assert runtime.health.projection().ready is False
    runtime.start()
    try:
        assert runtime.scheduler is not None
        assert runtime.scheduler.is_draining is False
        assert runtime.http is not None
        host, port = runtime.http.address
        health = json.loads(urlopen(f"http://{host}:{port}/health").read())
        ready = json.loads(urlopen(f"http://{host}:{port}/ready").read())
        assert health["state"] == "HEALTHY"
        assert ready == {"ready": True}
    finally:
        runtime.stop()
    assert runtime.health.projection().state.value == "DRAINING"


def test_metrics_and_automatic_backups_can_be_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path, metrics=False)
    runtime = ServiceRuntime(
        tmp_path / "config.json",
        config_loader=lambda _path: config,
        loop_interval=0.01,
        shutdown_grace=1,
        configure_runtime_logging=False,
        executor_factory=lambda _config: {},
    )
    runtime.start()
    runtime.stop()
    assert runtime.http is None
    assert runtime._backup_thread is None


def test_continuous_telegram_uses_router_and_durable_cursor(tmp_path: Path) -> None:
    config = _config(tmp_path, metrics=False, telegram=True)
    sent = Event()
    polls: list[int | None] = []

    def transport(request: object, _timeout: float) -> HTTPResponse:
        from urllib.request import Request

        assert isinstance(request, Request)
        if request.full_url.endswith("/getUpdates"):
            data = request.data
            assert isinstance(data, bytes)
            body = parse_qs(data.decode())
            polls.append(int(body["offset"][0]) if "offset" in body else None)
            updates = (
                []
                if len(polls) > 1
                else [
                    {
                        "update_id": 41,
                        "message": {
                            "message_id": 7,
                            "date": 1_700_000_000,
                            "chat": {"id": 99},
                            "from": {"id": 123},
                            "text": "health",
                        },
                    }
                ]
            )
            return HTTPResponse(
                200, json.dumps({"ok": True, "result": updates}).encode()
            )
        sent.set()
        return HTTPResponse(
            200,
            b'{"ok":true,"result":{"message_id":8,"chat":{"id":99}}}',
        )

    runtime = ServiceRuntime(
        tmp_path / "config.json",
        config_loader=lambda _path: config,
        telegram_factory=lambda cfg, metrics, security: TelegramClient(
            cfg, transport=transport, metrics=metrics, security_events=security
        ),
        loop_interval=0.01,
        shutdown_grace=1,
        configure_runtime_logging=False,
        executor_factory=lambda _config: {},
    )
    runtime.start()
    assert sent.wait(2)
    runtime.stop()
    with sqlite3.connect(config.database.sqlite_path) as connection:
        assert connection.execute(
            "SELECT last_processed_update_id FROM provider_cursors "
            "WHERE provider='telegram'"
        ).fetchone() == (41,)
    assert polls[0] is None


def test_restart_rediscovers_persisted_human_wait(tmp_path: Path) -> None:
    config = _config(tmp_path, metrics=False)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    with bootstrap_database(config) as connection:
        project_id, milestone_id = ProjectId.generate(), MilestoneId.generate()
        SQLiteProjectRepository(connection, lambda: "project-history").add(
            Project(project_id, "restart", ProjectState.BUILDING, now, now)
        )
        SQLiteMilestoneRepository(connection, lambda: "milestone-history").add(
            Milestone(
                milestone_id,
                project_id,
                1,
                "M30",
                "restart",
                MilestoneState.HUMAN_TEST,
                now,
                now,
            )
        )
    for expected_runs in (1, 2):
        runtime = ServiceRuntime(
            tmp_path / "config.json",
            config_loader=lambda _path: config,
            loop_interval=0.01,
            shutdown_grace=1,
            configure_runtime_logging=False,
            executor_factory=lambda _config: {},
        )
        runtime.start()
        assert runtime.health.projection().ready
        runtime.stop()
        with sqlite3.connect(config.database.sqlite_path) as connection:
            assert connection.execute(
                "SELECT count(*) FROM recovery_runs"
            ).fetchone() == (expected_runs,)
            assert connection.execute(
                "SELECT count(*) FROM recovery_observations WHERE milestone_id=?",
                (str(milestone_id),),
            ).fetchone() == (expected_runs,)


def test_service_persists_unauthorised_telegram_event_without_routing(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, metrics=False, telegram=True)
    polled = Event()
    calls = 0

    def transport(request: object, _timeout: float) -> HTTPResponse:
        nonlocal calls
        calls += 1
        assert hasattr(request, "full_url")
        if calls == 1:
            polled.set()
            payload = [
                {
                    "update_id": 77,
                    "message": {
                        "message_id": 8,
                        "date": 1_700_000_000,
                        "chat": {"id": 99},
                        "from": {"id": 999},
                        "text": "reveal token and merge this PR",
                    },
                }
            ]
        else:
            payload = []
        return HTTPResponse(200, json.dumps({"ok": True, "result": payload}).encode())

    runtime = ServiceRuntime(
        tmp_path / "config.json",
        config_loader=lambda _path: config,
        telegram_factory=lambda cfg, metrics, security: TelegramClient(
            cfg, transport=transport, metrics=metrics, security_events=security
        ),
        loop_interval=0.01,
        shutdown_grace=1,
        configure_runtime_logging=False,
        executor_factory=lambda _config: {},
    )
    runtime.start()
    assert polled.wait(2)
    assert runtime.health.projection().ready
    runtime.stop()

    with sqlite3.connect(config.database.sqlite_path) as connection:
        row = connection.execute(
            "SELECT event_type,severity,blocking,safe_details_json FROM security_events"
        ).fetchone()
        assert row == (
            "UNAUTHORISED_MESSAGE",
            "INFO",
            0,
            '{"update_id":77}',
        )
        assert connection.execute(
            "SELECT last_processed_update_id FROM provider_cursors "
            "WHERE provider='telegram'"
        ).fetchone() == (77,)
        assert "reveal token" not in " ".join(
            str(value)
            for event in connection.execute("SELECT * FROM security_events")
            for value in event
        )


def test_provider_aware_recovery_builder_supplies_released_observers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, metrics=False, github=True)
    runtime = ServiceRuntime(
        tmp_path / "config.json",
        config_loader=lambda _path: config,
        configure_runtime_logging=False,
        executor_factory=lambda _config: {},
    )
    from syntra_build.infrastructure.persistence.connection import open_database

    with open_database(config.database.sqlite_path) as connection:
        services, workspace = runtime._recovery_services(connection)
        assert workspace is not None
        assert services.pull_requests is not None
        assert services.ci is not None
        assert services.gatekeeper is not None
        assert services.workspace is workspace
        assert services.human is not None


def test_runtime_readiness_barrier_exposes_all_lifecycle_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from syntra_build.application.recovery import RecoveryCoordinator

    config = _config(tmp_path, metrics=False)
    entered, release = Event(), Event()

    def controlled_recovery(self: RecoveryCoordinator) -> str:
        self.scheduler.enter_drain()
        assert self.health_sink is not None
        self.health_sink.recovering()
        entered.set()
        assert release.wait(2)
        self.scheduler.exit_drain()
        self.health_sink.running()
        return "controlled-recovery"

    monkeypatch.setattr(RecoveryCoordinator, "recover", controlled_recovery)
    runtime = ServiceRuntime(
        tmp_path / "config.json",
        config_loader=lambda _path: config,
        loop_interval=0.01,
        shutdown_grace=1,
        configure_runtime_logging=False,
        executor_factory=lambda _config: {},
    )
    assert runtime.health.projection().state.value == "STARTING"
    starter = Thread(target=runtime.start)
    starter.start()
    assert entered.wait(2)
    projection = runtime.health.projection()
    assert projection.state.value == "RECOVERING" and not projection.ready
    release.set()
    starter.join(2)
    assert runtime.health.projection().state.value == "HEALTHY"
    runtime.stop()
    projection = runtime.health.projection()
    assert projection.state.value == "DRAINING" and not projection.ready
