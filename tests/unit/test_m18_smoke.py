from __future__ import annotations

import json
from pathlib import Path

import pytest

from syntra_build.infrastructure.config import (
    DEFAULT_ARCHITECT_API_KEY_PATH,
    DEFAULT_GITHUB_TOKEN_PATH,
    DEFAULT_HOST_CONFIG_PATH,
    DEFAULT_TELEGRAM_TOKEN_PATH,
    ConfigurationError,
)
from syntra_build.m18_smoke import load_m18_host_config

GITHUB_SECRET = "synthetic-m18-github-secret-never-output"


def _files(
    tmp_path: Path, *, enabled: bool = True, token: bool = True
) -> tuple[Path, Path]:
    config_path = tmp_path / "config.json"
    token_path = tmp_path / "github-token"
    config_path.write_text(
        json.dumps({"github": {"enabled": enabled, "owner": "Mac0z"}}),
        encoding="utf-8",
    )
    if token:
        token_path.write_text(GITHUB_SECRET, encoding="utf-8")
        token_path.chmod(0o600)
    return config_path, token_path


def test_m18_loads_github_host_config_and_protected_token(tmp_path: Path) -> None:
    config_path, token_path = _files(tmp_path)

    config = load_m18_host_config(
        config_path,
        github_token_path=token_path,
        telegram_token_path=tmp_path / "missing-telegram-token",
        architect_api_key_path=tmp_path / "missing-architect-key",
    )

    assert config.github.enabled
    assert config.github.owner == "Mac0z"
    assert config.secrets.github_token is not None
    assert config.secrets.github_token.value == GITHUB_SECRET
    assert config.filesystem.application_root == Path("/opt/syntra-build")
    assert config.filesystem.configuration_root == Path("/etc/syntra-build")
    assert config.filesystem.data_root == Path("/var/lib/syntra-build")
    assert GITHUB_SECRET not in repr(config)
    assert GITHUB_SECRET not in repr(config.secrets)


def test_m18_host_file_defaults_match_established_paths() -> None:
    assert DEFAULT_HOST_CONFIG_PATH == Path("/etc/syntra-build/config.json")
    assert DEFAULT_GITHUB_TOKEN_PATH == Path("/etc/syntra-build/github-token")
    assert DEFAULT_TELEGRAM_TOKEN_PATH == Path("/etc/syntra-build/telegram-token")
    assert DEFAULT_ARCHITECT_API_KEY_PATH == Path("/etc/syntra-build/openai-api-key")


def test_m18_rejects_missing_required_github_token_without_disclosure(
    tmp_path: Path,
) -> None:
    config_path, token_path = _files(tmp_path, token=False)

    with pytest.raises(ConfigurationError, match="GitHub token is required") as caught:
        load_m18_host_config(
            config_path,
            github_token_path=token_path,
            telegram_token_path=tmp_path / "missing-telegram-token",
            architect_api_key_path=tmp_path / "missing-architect-key",
        )
    assert GITHUB_SECRET not in str(caught.value)


def test_m18_rejects_disabled_github_configuration(tmp_path: Path) -> None:
    config_path, token_path = _files(tmp_path, enabled=False)

    with pytest.raises(RuntimeError, match="GitHub provisioning configuration"):
        load_m18_host_config(
            config_path,
            github_token_path=token_path,
            telegram_token_path=tmp_path / "missing-telegram-token",
            architect_api_key_path=tmp_path / "missing-architect-key",
        )


def test_m18_preserves_secret_file_permission_validation(tmp_path: Path) -> None:
    config_path, token_path = _files(tmp_path)
    token_path.chmod(0o640)

    with pytest.raises(RuntimeError, match="permissions are too broad") as caught:
        load_m18_host_config(
            config_path,
            github_token_path=token_path,
            telegram_token_path=tmp_path / "missing-telegram-token",
            architect_api_key_path=tmp_path / "missing-architect-key",
        )
    assert GITHUB_SECRET not in str(caught.value)
