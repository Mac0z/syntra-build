from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from syntra_build.adapters.telegram import TelegramInboundMessage
from syntra_build.infrastructure.config import ApplicationConfig, load_config
from syntra_build.smoke import (
    build_smoke_router,
    main,
    run_local_smoke,
    run_telegram_once,
)

REVISION = "a" * 40


def _config(root: Path) -> ApplicationConfig:
    app = root / "app"
    config = root / "config"
    data = root / "data"
    logs = root / "logs"
    for path in (app, config, data, logs):
        path.mkdir()
    return load_config(
        {
            "filesystem": {
                "application_root": str(app),
                "configuration_root": str(config),
                "data_root": str(data),
                "log_root": str(logs),
            },
            "database": {"sqlite_path": str(data / "syntra.db")},
        },
        environ={},
    )


def test_local_smoke_bootstraps_database_and_routes_ping(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls = 0

    def bootstrap(_config: ApplicationConfig) -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        connection = sqlite3.connect(tmp_path / "data" / "syntra.db")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    result = run_local_smoke(config, REVISION, bootstrap=bootstrap)

    assert calls == 1
    assert result.database == "OK"
    assert result.routing == "pong"


def test_local_smoke_fails_before_bootstrap_for_missing_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.filesystem.log_root.rmdir()
    called = False

    def bootstrap(_config: ApplicationConfig) -> sqlite3.Connection:
        nonlocal called
        called = True
        return sqlite3.connect(":memory:")

    try:
        run_local_smoke(config, REVISION, bootstrap=bootstrap)
    except RuntimeError as error:
        assert "filesystem root" in str(error)
    else:
        raise AssertionError("missing root was accepted")
    assert not called


class _FakeTelegramClient:
    def __init__(self, messages: tuple[TelegramInboundMessage, ...]) -> None:
        self.messages = messages
        self.polls = 0
        self.sent: list[dict[str, object]] = []

    def poll_updates(
        self, *, offset: int | None = None
    ) -> tuple[TelegramInboundMessage, ...]:
        self.polls += 1
        assert offset is None
        return self.messages

    def send_text(self, **values: object) -> object:
        self.sent.append(values)
        return object()


def test_telegram_once_polls_once_routes_and_preserves_destination() -> None:
    message = TelegramInboundMessage(
        update_id=11,
        message_id=22,
        chat_id=33,
        user_id=44,
        text="/ping",
        received_at=datetime.now(UTC),
        thread_id=55,
    )
    client = _FakeTelegramClient((message,))

    assert run_telegram_once(client, build_smoke_router()) == 1
    assert client.polls == 1
    assert client.sent == [
        {
            "chat_id": 33,
            "text": "pong",
            "thread_id": 55,
            "reply_to_message_id": 22,
        }
    ]


def test_telegram_once_empty_authorised_result_sends_nothing() -> None:
    client = _FakeTelegramClient(())
    assert run_telegram_once(client, build_smoke_router()) == 0
    assert client.polls == 1
    assert client.sent == []


def test_smoke_health_is_truthful_and_project_services_are_unavailable() -> None:
    router = build_smoke_router()
    message = TelegramInboundMessage(1, 2, 3, 4, "/health", datetime.now(UTC))
    client = _FakeTelegramClient((message,))

    run_telegram_once(client, router)

    assert client.sent[0]["text"] == (
        "development runtime available (local initialization passed)"
    )


def test_cli_failure_is_nonzero_and_secret_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.json"
    token_path = tmp_path / "telegram-token"
    revision_path = tmp_path / "REVISION"
    config_path.write_text("{}", encoding="utf-8")
    token_path.write_text("synthetic-secret-token", encoding="utf-8")
    token_path.chmod(0o644)
    revision_path.write_text(REVISION, encoding="ascii")

    result = main(
        [
            "local",
            "--config",
            str(config_path),
            "--token-file",
            str(token_path),
            "--revision-file",
            str(revision_path),
        ]
    )

    output = capsys.readouterr()
    assert result == 1
    assert "synthetic-secret-token" not in output.err
