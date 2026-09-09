from __future__ import annotations

import json
from pathlib import Path

import pytest

import syntra_build.architect_smoke as architect_smoke
from syntra_build.infrastructure.config import ConfigurationError

ARCHITECT_SECRET = "synthetic-architect-secret"
TELEGRAM_SECRET = "synthetic-telegram-secret"
GITHUB_SECRET = "synthetic-github-secret"


def _write_secret(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _host_files(
    tmp_path: Path, *, telegram: bool = False, github: bool = False
) -> tuple[Path, Path, Path, Path]:
    config_path = tmp_path / "config.json"
    architect_path = tmp_path / "openai-api-key"
    telegram_path = tmp_path / "telegram-token"
    github_path = tmp_path / "github-token"
    config_path.write_text(
        json.dumps(
            {
                "architect": {
                    "enabled": True,
                    "provider": "openai",
                    "model": "gpt-test",
                },
                "telegram": {
                    "enabled": telegram,
                    "authorised_user_ids": [123] if telegram else [],
                },
                "github": {"enabled": github, "owner": "example"},
            }
        ),
        encoding="utf-8",
    )
    _write_secret(architect_path, ARCHITECT_SECRET)
    if telegram:
        _write_secret(telegram_path, TELEGRAM_SECRET)
    if github:
        _write_secret(github_path, GITHUB_SECRET)
    return config_path, architect_path, telegram_path, github_path


@pytest.mark.parametrize(
    ("telegram", "github"),
    [(True, False), (False, True), (True, True)],
    ids=["telegram-enabled", "github-enabled", "both-enabled"],
)
def test_host_config_loads_every_enabled_integration_secret(
    tmp_path: Path, telegram: bool, github: bool
) -> None:
    config = architect_smoke._load_host_config(
        *_host_files(tmp_path, telegram=telegram, github=github)
    )

    assert config.secrets.architect_api_key is not None
    assert (config.secrets.telegram_bot_token is not None) is telegram
    assert (config.secrets.github_token is not None) is github


def test_enabled_integration_with_missing_secret_still_fails(tmp_path: Path) -> None:
    paths = _host_files(tmp_path, telegram=True)
    paths[2].unlink()

    with pytest.raises(ConfigurationError, match="Telegram token is required"):
        architect_smoke._load_host_config(*paths)


def test_smoke_secrets_are_redacted_and_provider_gets_only_architect_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = architect_smoke._load_host_config(
        *_host_files(tmp_path, telegram=True, github=True)
    )
    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(architect_smoke, "OpenAIArchitectProvider", FakeProvider)
    architect_smoke._build_provider(config)

    rendered = repr(config.secrets)
    assert set(captured) == {
        "api_key",
        "model",
        "reasoning_effort",
        "timeout_seconds",
    }
    assert captured["api_key"] == ARCHITECT_SECRET
    for secret in (ARCHITECT_SECRET, TELEGRAM_SECRET, GITHUB_SECRET):
        assert secret not in rendered
        assert secret not in str(
            {key: value for key, value in captured.items() if key != "api_key"}
        )


def test_additional_secret_files_require_private_permissions(tmp_path: Path) -> None:
    paths = _host_files(tmp_path, telegram=True)
    paths[2].chmod(0o640)

    with pytest.raises(RuntimeError, match="permissions are too broad") as caught:
        architect_smoke._load_host_config(*paths)
    assert TELEGRAM_SECRET not in str(caught.value)
