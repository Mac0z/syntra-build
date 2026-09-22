"""Shared loading of deployed host configuration and protected credentials."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from syntra_build.infrastructure.config.loader import (
    load_config,
    read_protected_secret_file,
)
from syntra_build.infrastructure.config.models import ApplicationConfig, SecretInputs

DEFAULT_HOST_CONFIG_PATH = Path("/etc/syntra-build/config.json")
DEFAULT_TELEGRAM_TOKEN_PATH = Path("/etc/syntra-build/telegram-token")
DEFAULT_GITHUB_TOKEN_PATH = Path("/etc/syntra-build/github-token")
DEFAULT_ARCHITECT_API_KEY_PATH = Path("/etc/syntra-build/openai-api-key")


def load_host_config(
    config_path: Path = DEFAULT_HOST_CONFIG_PATH,
    *,
    telegram_token_path: Path = DEFAULT_TELEGRAM_TOKEN_PATH,
    github_token_path: Path = DEFAULT_GITHUB_TOKEN_PATH,
    architect_api_key_path: Path = DEFAULT_ARCHITECT_API_KEY_PATH,
) -> ApplicationConfig:
    """Load the host JSON plus independently protected optional secret files."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise RuntimeError("configuration file must contain a JSON object")
    return load_config(
        raw,
        environ={},
        secrets=SecretInputs(
            architect_api_key=read_protected_secret_file(
                architect_api_key_path, "Architect API key"
            ),
            telegram_bot_token=read_protected_secret_file(
                telegram_token_path, "Telegram token"
            ),
            github_token=read_protected_secret_file(github_token_path, "GitHub token"),
        ),
    )
