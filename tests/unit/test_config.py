from __future__ import annotations

from pathlib import Path

import pytest

from syntra_build.infrastructure.config import (
    ConfigurationError,
    FilesystemConfig,
    ResourceThresholdConfig,
    SchedulerConfig,
    SecretInputs,
    SecretValue,
    TelegramConfig,
    load_config,
)


def development_paths(tmp_path: Path) -> dict[str, object]:
    return {
        "filesystem": {
            "application_root": tmp_path / "application",
            "configuration_root": tmp_path / "configuration",
            "data_root": tmp_path / "data",
            "log_root": tmp_path / "logs",
        }
    }


def test_default_configuration_loads_without_credentials() -> None:
    config = load_config(environ={})

    assert config.database.sqlite_path == Path("/var/lib/syntra-build/syntra.db")
    assert config.scheduler == SchedulerConfig()
    assert config.security == ResourceThresholdConfig()
    assert not config.telegram.enabled
    assert not config.architect.enabled
    assert not config.github.enabled


def test_development_paths_are_canonical_and_derived(tmp_path: Path) -> None:
    root = tmp_path / "parent" / ".." / "syntra"
    values = development_paths(root)

    config = load_config(values, environ={})

    assert config.filesystem.data_root == (tmp_path / "syntra/data").resolve()
    assert (
        config.filesystem.workspace_root
        == (tmp_path / "syntra/data/workspaces").resolve()
    )
    assert config.filesystem.backup_root == (tmp_path / "syntra/data/backups").resolve()
    assert config.database.sqlite_path == (tmp_path / "syntra/data/syntra.db").resolve()


@pytest.mark.parametrize("field", SchedulerConfig.__dataclass_fields__)
def test_scheduler_rejects_non_positive_concurrency(field: str) -> None:
    if field == "MAX_CONCURRENCY":
        return
    values = {field: 0}
    with pytest.raises(ConfigurationError, match=field):
        SchedulerConfig(**values)


def test_loader_rejects_invalid_timeout_immediately(tmp_path: Path) -> None:
    values = development_paths(tmp_path)
    values["codex"] = {"execution_timeout_seconds": 0}

    with pytest.raises(ConfigurationError, match="execution_timeout_seconds"):
        load_config(values, environ={})


def test_loader_rejects_invalid_retry_attempt_count(tmp_path: Path) -> None:
    values = development_paths(tmp_path)
    values["retries"] = {"infrastructure_attempts": "not-an-integer"}

    with pytest.raises(ConfigurationError, match="infrastructure_attempts"):
        load_config(values, environ={})


def test_incoherent_disk_thresholds_are_rejected() -> None:
    with pytest.raises(ConfigurationError, match="critical < stop Codex < warning"):
        ResourceThresholdConfig(
            disk_warning_percent_free=10,
            disk_stop_codex_percent_free=20,
            disk_critical_percent_free=5,
        )


def test_relative_paths_are_rejected() -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        FilesystemConfig(data_root=Path("relative"))


def test_sensitive_and_workspace_roots_must_not_overlap(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="must not overlap"):
        FilesystemConfig(
            application_root=tmp_path / "application",
            configuration_root=tmp_path / "configuration",
            data_root=tmp_path / "data",
            log_root=tmp_path / "logs",
            workspace_root=tmp_path / "application/workspaces",
            backup_root=tmp_path / "data/backups",
        )


def test_control_plane_roots_must_not_overlap(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigurationError, match="application_root.*configuration_root"
    ):
        FilesystemConfig(
            application_root=tmp_path / "control",
            configuration_root=tmp_path / "control/configuration",
            data_root=tmp_path / "data",
            log_root=tmp_path / "logs",
            workspace_root=tmp_path / "data/workspaces",
            backup_root=tmp_path / "data/backups",
        )


@pytest.mark.parametrize("user_ids", [(0,), (-4,), (True,), (1, 1)])
def test_malformed_telegram_ids_are_rejected(user_ids: tuple[int, ...]) -> None:
    with pytest.raises(ConfigurationError, match="authorised_user_ids"):
        TelegramConfig(authorised_user_ids=user_ids)


def test_safe_inspection_excludes_synthetic_secrets(tmp_path: Path) -> None:
    synthetic_token = "synthetic-test-token-not-a-real-secret"
    values = development_paths(tmp_path)
    secrets = SecretInputs(telegram_bot_token=SecretValue(synthetic_token))

    config = load_config(values, environ={}, secrets=secrets)
    inspected = repr(config)
    serialised = repr(config.safe_dict())

    assert synthetic_token not in inspected
    assert synthetic_token not in repr(secrets)
    assert synthetic_token not in serialised
    assert "secrets" not in config.safe_dict()


def test_ordinary_configuration_rejects_secrets(tmp_path: Path) -> None:
    values = development_paths(tmp_path)
    values["secrets"] = {"github_token": "synthetic-secret"}

    with pytest.raises(ConfigurationError, match="secrets must be supplied separately"):
        load_config(values, environ={})


def test_explicit_secret_inputs_satisfy_enabled_integration(tmp_path: Path) -> None:
    values = development_paths(tmp_path)
    values["github"] = {"enabled": True, "owner": "Mac0z"}
    token = SecretValue("synthetic-explicit-github-token")

    config = load_config(
        values,
        environ={"SYNTRA_GITHUB_TOKEN": "ignored-environment-token"},
        secrets=SecretInputs(github_token=token),
    )

    assert config.secrets.github_token is token


def test_environment_secret_satisfies_enabled_integration(tmp_path: Path) -> None:
    values = development_paths(tmp_path)
    values["github"] = {"enabled": True, "owner": "Mac0z"}

    config = load_config(
        values,
        environ={"SYNTRA_GITHUB_TOKEN": "synthetic-environment-github-token"},
    )

    assert config.secrets.github_token is not None


def test_explicit_values_override_environment_deterministically(tmp_path: Path) -> None:
    values = development_paths(tmp_path)
    values["scheduler"] = {"codex_concurrency": 3}

    config = load_config(
        values,
        environ={
            "SYNTRA_CODEX_CONCURRENCY": "7",
            "UNRELATED_ENVIRONMENT_VARIABLE": "ignored",
        },
    )

    assert config.scheduler.codex_concurrency == 3


def test_environment_values_are_parsed_centrally(tmp_path: Path) -> None:
    values = development_paths(tmp_path)
    config = load_config(
        values,
        environ={
            "SYNTRA_TELEGRAM_ENABLED": "yes",
            "SYNTRA_TELEGRAM_AUTHORISED_USER_IDS": "123,456",
            "SYNTRA_TELEGRAM_TOKEN": "synthetic-telegram-token",
        },
    )

    assert config.telegram.enabled
    assert config.telegram.authorised_user_ids == (123, 456)


def test_enabled_integration_requires_ordinary_settings_and_secret(
    tmp_path: Path,
) -> None:
    values = development_paths(tmp_path)
    values["github"] = {"enabled": True}

    with pytest.raises(ConfigurationError, match="github.owner"):
        load_config(values, environ={})


def test_invalid_environment_value_fails_during_loading(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="codex_concurrency"):
        load_config(
            development_paths(tmp_path),
            environ={"SYNTRA_CODEX_CONCURRENCY": "invalid"},
        )
