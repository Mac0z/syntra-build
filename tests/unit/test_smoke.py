from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from syntra_build.adapters.github import GitHubHTTPResponse
from syntra_build.adapters.telegram import (
    TelegramInboundMessage,
    TelegramPolledUpdate,
    TelegramUpdateDisposition,
)
from syntra_build.application.commands import InboundMessage
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)
from syntra_build.infrastructure.persistence import bootstrap_database
from syntra_build.smoke import (
    build_host_router,
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


def _routable(message: TelegramInboundMessage) -> TelegramPolledUpdate:
    return TelegramPolledUpdate(
        message.update_id, TelegramUpdateDisposition.ROUTABLE, message
    )


class _FakeCursors:
    def __init__(self, value: int | None = None) -> None:
        self.value = value

    def get(self, provider: str) -> int | None:
        assert provider == "telegram"
        return self.value

    def advance(self, provider: str, update_id: int) -> int:
        assert provider == "telegram"
        self.value = update_id if self.value is None else max(self.value, update_id)
        return self.value


class _FakeTelegramClient:
    def __init__(self, messages: tuple[TelegramInboundMessage, ...]) -> None:
        self.updates = tuple(_routable(message) for message in messages)
        self.polls = 0
        self.offsets: list[int | None] = []
        self.sent: list[dict[str, object]] = []

    def poll_updates(
        self, *, offset: int | None = None
    ) -> tuple[TelegramPolledUpdate, ...]:
        self.polls += 1
        self.offsets.append(offset)
        return self.updates

    def send_text(self, **values: object) -> object:
        self.sent.append(values)
        return object()


class _FailingSecondSendClient(_FakeTelegramClient):
    def send_text(self, **values: object) -> object:
        if len(self.sent) == 1:
            raise RuntimeError("synthetic send failure")
        return super().send_text(**values)


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

    cursors = _FakeCursors()
    assert run_telegram_once(client, build_smoke_router(), cursors) == 1
    assert client.polls == 1
    assert client.offsets == [None]
    assert client.sent == [
        {
            "chat_id": 33,
            "text": "pong",
            "thread_id": 55,
            "reply_to_message_id": 22,
        }
    ]
    assert cursors.value == 11


def test_successful_updates_advance_to_highest_and_next_poll_uses_successor() -> None:
    messages = tuple(
        TelegramInboundMessage(
            update_id=value,
            message_id=value,
            chat_id=33,
            user_id=44,
            text="/ping",
            received_at=datetime.now(UTC),
        )
        for value in (101, 102)
    )
    cursors = _FakeCursors(100)
    assert (
        run_telegram_once(_FakeTelegramClient(messages), build_smoke_router(), cursors)
        == 2
    )
    assert cursors.value == 102

    fresh_client = _FakeTelegramClient(())
    assert run_telegram_once(fresh_client, build_smoke_router(), cursors) == 0
    assert fresh_client.offsets == [103]
    assert cursors.value == 102


def test_telegram_once_empty_authorised_result_sends_nothing() -> None:
    client = _FakeTelegramClient(())
    assert run_telegram_once(client, build_smoke_router(), _FakeCursors(100)) == 0
    assert client.polls == 1
    assert client.offsets == [101]
    assert client.sent == []


def test_telegram_once_stops_at_failure_and_replays_from_failed_update() -> None:
    messages = tuple(
        TelegramInboundMessage(
            update_id=value,
            message_id=value,
            chat_id=33,
            user_id=44,
            text="/ping",
            received_at=datetime.now(UTC),
        )
        for value in (101, 102, 103)
    )
    cursors = _FakeCursors(100)
    client = _FailingSecondSendClient(messages)
    with pytest.raises(RuntimeError, match="synthetic send failure"):
        run_telegram_once(client, build_smoke_router(), cursors)
    assert cursors.value == 101
    assert len(client.sent) == 1

    retry = _FakeTelegramClient(())
    assert run_telegram_once(retry, build_smoke_router(), cursors) == 0
    assert retry.offsets == [102]


@pytest.mark.parametrize(
    "disposition",
    [TelegramUpdateDisposition.UNSUPPORTED, TelegramUpdateDisposition.UNAUTHORISED],
)
def test_deliberately_ignored_update_advances_without_routing(
    disposition: TelegramUpdateDisposition,
) -> None:
    client = _FakeTelegramClient(())
    client.updates = (TelegramPolledUpdate(101, disposition),)
    cursors = _FakeCursors(100)
    assert run_telegram_once(client, build_smoke_router(), cursors) == 0
    assert cursors.value == 101
    assert client.sent == []


def test_smoke_health_is_truthful_and_project_services_are_unavailable() -> None:
    router = build_smoke_router()
    message = TelegramInboundMessage(1, 2, 3, 4, "/health", datetime.now(UTC))
    client = _FakeTelegramClient((message,))

    run_telegram_once(client, router, _FakeCursors())

    assert client.sent[0]["text"] == (
        "development runtime available (local initialization passed)"
    )


def test_real_host_router_composes_m14_github_check_and_persistence(
    tmp_path: Path,
) -> None:
    base = _config(tmp_path)
    config = load_config(
        {
            **base.safe_dict(),
            "github": {"enabled": True, "owner": "Mac0z"},
        },
        environ={},
        secrets=SecretInputs(github_token=SecretValue("synthetic-github-token")),
    )
    connection = bootstrap_database(config)
    router = build_host_router(
        config,
        connection,
        github_transport=lambda _request, _timeout: GitHubHTTPResponse(404, b"{}"),
    )
    response = router.route(
        InboundMessage(
            "telegram",
            "101",
            "201",
            "301",
            datetime.now(UTC),
            "/create Host Project | Verify real composition",
            "401",
        )
    )
    assert "State: DESIGNING" in response.text
    assert (
        router.route(
            InboundMessage(
                "telegram",
                "102",
                "202",
                "301",
                datetime.now(UTC),
                "/status Host Project",
                "401",
            )
        ).text
        == "Project: Host Project\nState: DESIGNING"
    )
    connection.close()


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
