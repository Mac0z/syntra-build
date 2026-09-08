from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import Request

import pytest

from syntra_build.adapters.telegram import (
    HTTPResponse,
    TelegramAPIError,
    TelegramClient,
    TelegramInboundMessage,
    TelegramProtocolError,
    TelegramTransportError,
)
from syntra_build.infrastructure.config import SecretInputs, SecretValue, load_config

TOKEN = "synthetic-telegram-token-never-log"


def client(
    tmp_path: Path, transport: Callable[[Request, float], HTTPResponse]
) -> TelegramClient:
    config = load_config(
        {
            "filesystem": {
                "application_root": tmp_path / "application",
                "configuration_root": tmp_path / "configuration",
                "data_root": tmp_path / "data",
                "log_root": tmp_path / "logs",
            },
            "telegram": {
                "enabled": True,
                "authorised_user_ids": [42, 43],
                "polling_timeout_seconds": 17,
            },
        },
        environ={},
        secrets=SecretInputs(telegram_bot_token=SecretValue(TOKEN)),
    )
    return TelegramClient(config, transport=transport)


def response(result: object, *, ok: bool = True) -> HTTPResponse:
    import json

    return HTTPResponse(200, json.dumps({"ok": ok, "result": result}).encode())


def update(
    *,
    update_id: int = 100,
    user_id: int = 42,
    message_id: int = 200,
    text: str = "private message body",
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id, "username": "not-an-identity"},
            "chat": {"id": 300},
            "date": 1_700_000_000,
            "text": text,
        },
    }


def request_parameters(request: Request) -> dict[str, list[str]]:
    assert isinstance(request.data, bytes)
    return parse_qs(request.data.decode())


def capture_adapter_logs(caplog: pytest.LogCaptureFixture) -> logging.Logger:
    """Attach pytest's handler despite the configured namespace not propagating."""
    logger = logging.getLogger("syntra_build.adapters.telegram")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.INFO)
    return logger


def test_authorised_text_update_is_normalised_in_utc(tmp_path: Path) -> None:
    gateway = client(tmp_path, lambda _request, _timeout: response([update()]))

    assert gateway.poll_updates() == (
        TelegramInboundMessage(
            update_id=100,
            message_id=200,
            chat_id=300,
            user_id=42,
            text="private message body",
            received_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
        ),
    )


def test_unauthorised_and_unsupported_updates_are_discarded(tmp_path: Path) -> None:
    unsupported = {"update_id": 102, "edited_message": {"text": "ignored"}}
    gateway = client(
        tmp_path,
        lambda _request, _timeout: response(
            [update(update_id=101, user_id=999), unsupported]
        ),
    )

    assert gateway.poll_updates() == ()


def test_multiple_updates_preserve_provider_order_and_optional_fields(
    tmp_path: Path,
) -> None:
    first = update(update_id=2, user_id=43)
    second = update(update_id=1)
    second_message = second["message"]
    assert isinstance(second_message, dict)
    second_message["message_thread_id"] = 8
    second_message["reply_to_message"] = {"message_id": 9}
    gateway = client(tmp_path, lambda _request, _timeout: response([first, second]))

    messages = gateway.poll_updates()

    assert [item.update_id for item in messages] == [2, 1]
    assert messages[0].thread_id is None
    assert messages[0].reply_to_message_id is None
    assert messages[1].thread_id == 8
    assert messages[1].reply_to_message_id == 9


def test_poll_sends_offset_and_configured_timeout(tmp_path: Path) -> None:
    observed: list[tuple[dict[str, list[str]], float]] = []

    def transport(request: Request, timeout: float) -> HTTPResponse:
        observed.append((request_parameters(request), timeout))
        return response([])

    client(tmp_path, transport).poll_updates(offset=501)

    assert observed == [({"timeout": ["17.0"], "offset": ["501"]}, 27.0)]


def test_gateway_has_no_hidden_deduplication(tmp_path: Path) -> None:
    gateway = client(tmp_path, lambda _request, _timeout: response([update()]))

    assert gateway.poll_updates(offset=100) == gateway.poll_updates(offset=100)


@pytest.mark.parametrize(
    "provider_response",
    [
        HTTPResponse(200, b"not-json"),
        HTTPResponse(200, b"[]"),
        HTTPResponse(200, b'{"result": []}'),
        HTTPResponse(200, b'{"ok": true, "result": {}}'),
    ],
)
def test_malformed_responses_raise_protocol_error(
    tmp_path: Path, provider_response: HTTPResponse
) -> None:
    gateway = client(tmp_path, lambda _request, _timeout: provider_response)

    with pytest.raises(TelegramProtocolError):
        gateway.poll_updates()


def test_api_failure_is_typed_and_safe(tmp_path: Path) -> None:
    provider_response = HTTPResponse(
        200,
        b'{"ok": false, "error_code": 429, "description": "Too Many Requests"}',
    )
    gateway = client(tmp_path, lambda _request, _timeout: provider_response)

    with pytest.raises(TelegramAPIError, match="429.*Too Many Requests") as captured:
        gateway.poll_updates()

    assert captured.value.error_code == 429
    assert TOKEN not in str(captured.value)


def test_network_failure_is_wrapped_chained_and_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cause = OSError(f"network down near {TOKEN}")

    def failing_transport(_request: Request, _timeout: float) -> HTTPResponse:
        raise cause

    gateway = client(tmp_path, failing_transport)
    logger = capture_adapter_logs(caplog)
    try:
        with pytest.raises(TelegramTransportError) as captured:
            gateway.poll_updates()
    finally:
        logger.removeHandler(caplog.handler)

    assert captured.value.__cause__ is cause
    assert TOKEN not in str(captured.value)
    assert TOKEN not in caplog.text
    assert any(
        getattr(record, "event", None) == "telegram_transport_failed"
        for record in caplog.records
    )


def test_send_text_posts_optional_fields_and_normalises_result(tmp_path: Path) -> None:
    observed: list[tuple[str, dict[str, list[str]], float]] = []

    def transport(request: Request, timeout: float) -> HTTPResponse:
        observed.append((request.full_url, request_parameters(request), timeout))
        return response({"message_id": 91, "chat": {"id": 300}, "message_thread_id": 7})

    sent = client(tmp_path, transport).send_text(
        chat_id=300,
        text="hello",
        thread_id=7,
        reply_to_message_id=8,
    )

    assert sent.message_id == 91
    assert sent.chat_id == 300
    assert sent.thread_id == 7
    _, parameters, timeout = observed[0]
    assert parameters == {
        "chat_id": ["300"],
        "text": ["hello"],
        "message_thread_id": ["7"],
        "reply_to_message_id": ["8"],
    }
    assert timeout == 10.0


def test_lifecycle_logs_exclude_text_and_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gateway = client(tmp_path, lambda _request, _timeout: response([update()]))

    logger = capture_adapter_logs(caplog)
    try:
        gateway.poll_updates()
    finally:
        logger.removeHandler(caplog.handler)

    events = {getattr(record, "event", None) for record in caplog.records}
    assert {
        "telegram_poll_started",
        "telegram_update_received",
        "telegram_poll_completed",
    } <= events
    assert "private message body" not in caplog.text
    assert TOKEN not in caplog.text


def test_protocol_and_api_failures_do_not_expose_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    failures = [
        HTTPResponse(200, b"invalid-json"),
        HTTPResponse(
            200,
            ('{"ok": false, "description": "denied ' + TOKEN + '"}').encode(),
        ),
    ]
    for provider_response in failures:
        gateway = client(tmp_path, lambda _request, _timeout: provider_response)
        with pytest.raises((TelegramProtocolError, TelegramAPIError)) as captured:
            gateway.poll_updates()
        assert TOKEN not in str(captured.value)
    assert TOKEN not in caplog.text
