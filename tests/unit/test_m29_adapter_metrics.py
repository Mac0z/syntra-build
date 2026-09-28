from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from syntra_build.adapters.architect import OpenAIArchitectProvider
from syntra_build.adapters.github.actions import CIProviderError, GitHubActionsAdapter
from syntra_build.adapters.github.repository_names import GitHubHTTPResponse
from syntra_build.adapters.telegram import (
    HTTPResponse,
    TelegramAPIError,
    TelegramClient,
    TelegramProtocolError,
    TelegramTransportError,
)
from syntra_build.adapters.telegram.client import HTTPTransport
from syntra_build.application.architect import ArchitectError, build_design_request
from syntra_build.application.metrics import APIFailureClassification, APIProvider
from syntra_build.domain import Project, ProjectDesignContext, ProjectId, ProjectState
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)
from syntra_build.infrastructure.metrics import PrometheusRecorder

SECRET = "credential-DO-NOT-EXPORT"


def config(tmp_path: Path) -> ApplicationConfig:
    data = tmp_path / "data"
    data.mkdir()
    return load_config(
        {
            "filesystem": {
                "application_root": tmp_path / "app",
                "configuration_root": tmp_path / "config",
                "data_root": data,
                "log_root": tmp_path / "logs",
            },
            "database": {"sqlite_path": data / "db"},
            "telegram": {"enabled": True, "authorised_user_ids": [42]},
            "github": {"enabled": True, "owner": "owner"},
        },
        environ={},
        secrets=SecretInputs(
            telegram_bot_token=SecretValue(SECRET),
            github_token=SecretValue(SECRET),
        ),
    )


@pytest.mark.parametrize(
    ("transport", "error", "classification"),
    [
        (
            lambda _r, _t: (_ for _ in ()).throw(OSError("RAW_TRANSPORT_SECRET")),
            TelegramTransportError,
            APIFailureClassification.TRANSPORT,
        ),
        (
            lambda _r, _t: HTTPResponse(
                429, b'{"ok":false,"description":"RAW_API_SECRET"}'
            ),
            TelegramAPIError,
            APIFailureClassification.REJECTION,
        ),
        (
            lambda _r, _t: HTTPResponse(200, b"RAW_PROTOCOL_SECRET"),
            TelegramProtocolError,
            APIFailureClassification.PROTOCOL,
        ),
    ],
)
def test_real_telegram_failures_record_once(
    tmp_path: Path,
    transport: HTTPTransport,
    error: type[Exception],
    classification: APIFailureClassification,
) -> None:
    metrics = PrometheusRecorder()
    client = TelegramClient(config(tmp_path), transport=transport, metrics=metrics)
    with pytest.raises(error):
        client.poll_updates()
    assert metrics.api_failures == {
        (APIProvider.TELEGRAM.value, classification.value): 1
    }
    exported = "\n".join(metrics.samples())
    assert "RAW_" not in exported and SECRET not in exported


@pytest.mark.parametrize(
    ("response", "classification"),
    [
        (
            GitHubHTTPResponse(401, b"RAW_AUTH_SECRET"),
            APIFailureClassification.AUTHENTICATION,
        ),
        (
            GitHubHTTPResponse(500, b"RAW_TRANSIENT_SECRET"),
            APIFailureClassification.TRANSIENT,
        ),
        (
            GitHubHTTPResponse(422, b"RAW_REJECTION_SECRET"),
            APIFailureClassification.REJECTION,
        ),
        (
            GitHubHTTPResponse(200, b"RAW_MALFORMED_SECRET"),
            APIFailureClassification.MALFORMED,
        ),
    ],
)
def test_real_github_failures_record_once(
    tmp_path: Path,
    response: GitHubHTTPResponse,
    classification: APIFailureClassification,
) -> None:
    metrics = PrometheusRecorder()
    adapter = GitHubActionsAdapter(
        config(tmp_path), transport=lambda _r, _t: response, metrics=metrics
    )
    with pytest.raises(CIProviderError):
        adapter.observe("owner/repository-do-not-export", 7, "a" * 40)
    assert metrics.api_failures == {(APIProvider.GITHUB.value, classification.value): 1}
    exported = "\n".join(metrics.samples())
    assert "RAW_" not in exported and SECRET not in exported


def test_real_architect_failure_records_once() -> None:
    metrics = PrometheusRecorder()
    provider = OpenAIArchitectProvider(
        api_key=SECRET,
        model="model",
        transport=lambda _payload, _key, _timeout: (_ for _ in ()).throw(
            TimeoutError("RAW_ARCHITECT_SECRET")
        ),
        metrics=metrics,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    project = Project(
        ProjectId.generate(),
        "SECRET_PROJECT_DO_NOT_EXPORT",
        ProjectState.DESIGNING,
        now,
        now,
    )
    request = build_design_request(
        ProjectDesignContext(project, None, (), (), ()), "correlation"
    )
    with pytest.raises(ArchitectError):
        provider.design(request)
    assert metrics.api_failures == {
        (APIProvider.ARCHITECT.value, APIFailureClassification.TRANSIENT.value): 1
    }
    exported = "\n".join(metrics.samples())
    assert (
        "RAW_" not in exported
        and SECRET not in exported
        and project.name not in exported
    )


def test_telegram_post_decode_malformed_poll_records_once(tmp_path: Path) -> None:
    metrics = PrometheusRecorder()

    def transport(_request: object, _timeout: float) -> HTTPResponse:
        return HTTPResponse(200, b'{"ok":true,"result":{"raw":"PRIVATE"}}')

    client = TelegramClient(config(tmp_path), transport=transport, metrics=metrics)
    with pytest.raises(TelegramProtocolError):
        client.poll_updates()
    assert metrics.api_failures == {
        (APIProvider.TELEGRAM.value, APIFailureClassification.PROTOCOL.value): 1
    }
    assert "PRIVATE" not in "\n".join(metrics.samples())


def test_telegram_post_decode_malformed_send_records_once(tmp_path: Path) -> None:
    metrics = PrometheusRecorder()

    def transport(_request: object, _timeout: float) -> HTTPResponse:
        return HTTPResponse(200, b'{"ok":true,"result":{"message_id":"PRIVATE"}}')

    client = TelegramClient(config(tmp_path), transport=transport, metrics=metrics)
    with pytest.raises(TelegramProtocolError):
        client.send_text(chat_id=1, text="human text must not export")
    assert metrics.api_failures == {
        (APIProvider.TELEGRAM.value, APIFailureClassification.PROTOCOL.value): 1
    }
    exported = "\n".join(metrics.samples())
    assert "PRIVATE" not in exported and "human text must not export" not in exported


def test_github_post_decode_malformed_observation_records_once(tmp_path: Path) -> None:
    metrics = PrometheusRecorder()
    response = GitHubHTTPResponse(
        200, b'{"workflow_runs":{"raw":"PRIVATE_PROVIDER_VALUE"}}'
    )
    adapter = GitHubActionsAdapter(
        config(tmp_path), transport=lambda _r, _t: response, metrics=metrics
    )
    with pytest.raises(CIProviderError):
        adapter.observe("owner/private-repository", 7, "a" * 40)
    assert metrics.api_failures == {
        (APIProvider.GITHUB.value, APIFailureClassification.MALFORMED.value): 1
    }
    exported = "\n".join(metrics.samples())
    assert "PRIVATE_PROVIDER_VALUE" not in exported
    assert "private-repository" not in exported
