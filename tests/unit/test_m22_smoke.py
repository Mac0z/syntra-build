from __future__ import annotations

from pathlib import Path

import pytest

from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)
from syntra_build.m22_smoke import trusted_git_from_host_config


def _config(tmp_path: Path) -> tuple[ApplicationConfig, SecretValue]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    token = SecretValue("synthetic-m22-token")
    config = load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "application"),
                "configuration_root": str(tmp_path / "configuration"),
                "data_root": str(data_root),
                "log_root": str(tmp_path / "logs"),
            },
            "database": {"sqlite_path": str(data_root / "syntra.db")},
            "github": {"enabled": True, "owner": "synthetic-owner"},
        },
        environ={},
        secrets=SecretInputs(github_token=token),
    )
    return config, token


def test_smoke_builds_authenticated_trusted_git_from_host_config(
    tmp_path: Path,
) -> None:
    config, token = _config(tmp_path)
    trusted_git = trusted_git_from_host_config(config, config.filesystem.data_root)
    assert trusted_git.username == "synthetic-owner"
    assert trusted_git.token is token
    assert (
        trusted_git.authentication_root
        == (config.filesystem.data_root / "authentication").resolve()
    )


def test_smoke_rejects_a_cli_data_root_that_differs_from_config(tmp_path: Path) -> None:
    config, _ = _config(tmp_path)
    with pytest.raises(ValueError, match="must match"):
        trusted_git_from_host_config(config, tmp_path / "different-data")
