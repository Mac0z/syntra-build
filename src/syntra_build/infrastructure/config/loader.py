"""Central deterministic configuration loader."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from syntra_build.infrastructure.config.models import (
    ApplicationConfig,
    ArchitectConfig,
    BackupConfig,
    CodexConfig,
    ConfigurationError,
    DatabaseConfig,
    FilesystemConfig,
    GitHubConfig,
    LoggingConfig,
    MetricsConfig,
    ResourceThresholdConfig,
    RetryConfig,
    SchedulerConfig,
    SecretInputs,
    SecretValue,
    TelegramConfig,
)

ENVIRONMENT_KEYS = frozenset(
    {
        "SYNTRA_APPLICATION_ROOT",
        "SYNTRA_CONFIGURATION_ROOT",
        "SYNTRA_DATA_ROOT",
        "SYNTRA_LOG_ROOT",
        "SYNTRA_WORKSPACE_ROOT",
        "SYNTRA_BACKUP_ROOT",
        "SYNTRA_DATABASE_PATH",
        "SYNTRA_TELEGRAM_ENABLED",
        "SYNTRA_TELEGRAM_AUTHORISED_USER_IDS",
        "SYNTRA_TELEGRAM_TOKEN",
        "SYNTRA_ARCHITECT_ENABLED",
        "SYNTRA_ARCHITECT_PROVIDER",
        "SYNTRA_ARCHITECT_MODEL",
        "SYNTRA_ARCHITECT_API_KEY",
        "SYNTRA_CODEX_CREDENTIAL",
        "SYNTRA_GITHUB_ENABLED",
        "SYNTRA_GITHUB_OWNER",
        "SYNTRA_GITHUB_TOKEN",
        "SYNTRA_ARCHITECT_CONCURRENCY",
        "SYNTRA_CODEX_CONCURRENCY",
        "SYNTRA_REPOSITORY_PROVISIONING_CONCURRENCY",
        "SYNTRA_MERGE_CONCURRENCY",
        "SYNTRA_INFRASTRUCTURE_RETRY_ATTEMPTS",
        "SYNTRA_CODEX_TIMEOUT_SECONDS",
    }
)


def _bool(name: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "1", "yes"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "0", "no"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


def _int(name: str, value: object) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be an integer")
    try:
        return int(str(value))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error


def _float(name: str, value: object) -> float:
    try:
        return float(str(value))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a number") from error


def _group(values: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = values.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} configuration must be a mapping")
    return value


def _pick(
    explicit: Mapping[str, object],
    key: str,
    environment: Mapping[str, str],
    environment_key: str,
    default: object,
) -> object:
    if key in explicit:
        return explicit[key]
    return environment.get(environment_key, default)


def _secret(value: object) -> SecretValue | None:
    if value is None:
        return None
    if isinstance(value, SecretValue):
        return value
    return SecretValue(str(value))


def load_config(
    values: Mapping[str, object] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    secrets: SecretInputs | None = None,
) -> ApplicationConfig:
    """Load one validated config; explicit values take precedence over environment."""
    source = values or {}
    if "secrets" in source:
        raise ConfigurationError(
            "secrets must be supplied separately through SecretInputs or dedicated "
            "secret environment variables"
        )
    env = os.environ if environ is None else environ
    fs = _group(source, "filesystem")
    defaults = FilesystemConfig()
    data_root = Path(
        str(_pick(fs, "data_root", env, "SYNTRA_DATA_ROOT", defaults.data_root))
    )
    filesystem = FilesystemConfig(
        application_root=Path(
            str(
                _pick(
                    fs,
                    "application_root",
                    env,
                    "SYNTRA_APPLICATION_ROOT",
                    defaults.application_root,
                )
            )
        ),
        configuration_root=Path(
            str(
                _pick(
                    fs,
                    "configuration_root",
                    env,
                    "SYNTRA_CONFIGURATION_ROOT",
                    defaults.configuration_root,
                )
            )
        ),
        data_root=data_root,
        log_root=Path(
            str(_pick(fs, "log_root", env, "SYNTRA_LOG_ROOT", defaults.log_root))
        ),
        workspace_root=Path(
            str(
                _pick(
                    fs,
                    "workspace_root",
                    env,
                    "SYNTRA_WORKSPACE_ROOT",
                    data_root / "workspaces",
                )
            )
        ),
        backup_root=Path(
            str(
                _pick(
                    fs,
                    "backup_root",
                    env,
                    "SYNTRA_BACKUP_ROOT",
                    data_root / "backups",
                )
            )
        ),
    )

    loaded_secrets = secrets or SecretInputs(
        telegram_bot_token=_secret(env.get("SYNTRA_TELEGRAM_TOKEN")),
        architect_api_key=_secret(env.get("SYNTRA_ARCHITECT_API_KEY")),
        codex_provider_credential=_secret(env.get("SYNTRA_CODEX_CREDENTIAL")),
        github_token=_secret(env.get("SYNTRA_GITHUB_TOKEN")),
    )
    database = _group(source, "database")
    telegram = _group(source, "telegram")
    architect = _group(source, "architect")
    codex = _group(source, "codex")
    github = _group(source, "github")
    scheduler = _group(source, "scheduler")
    retries = _group(source, "retries")
    logging = _group(source, "logging")
    metrics = _group(source, "metrics")
    backups = _group(source, "backups")
    security = _group(source, "security")

    telegram_ids_value = _pick(
        telegram, "authorised_user_ids", env, "SYNTRA_TELEGRAM_AUTHORISED_USER_IDS", ()
    )
    if isinstance(telegram_ids_value, str):
        telegram_ids: tuple[object, ...] = tuple(
            item.strip() for item in telegram_ids_value.split(",") if item.strip()
        )
    elif isinstance(telegram_ids_value, (list, tuple)):
        telegram_ids = tuple(telegram_ids_value)
    else:
        raise ConfigurationError(
            "telegram.authorised_user_ids must be a list or comma-separated string"
        )

    return ApplicationConfig(
        filesystem=filesystem,
        database=DatabaseConfig(
            Path(
                str(
                    _pick(
                        database,
                        "sqlite_path",
                        env,
                        "SYNTRA_DATABASE_PATH",
                        filesystem.data_root / "syntra.db",
                    )
                )
            )
        ),
        telegram=TelegramConfig(
            enabled=_bool(
                "telegram.enabled",
                _pick(telegram, "enabled", env, "SYNTRA_TELEGRAM_ENABLED", False),
            ),
            authorised_user_ids=tuple(
                _int("telegram.authorised_user_ids", value) for value in telegram_ids
            ),
            polling_timeout_seconds=_float(
                "telegram.polling_timeout_seconds",
                telegram.get("polling_timeout_seconds", 30.0),
            ),
        ),
        architect=ArchitectConfig(
            enabled=_bool(
                "architect.enabled",
                _pick(architect, "enabled", env, "SYNTRA_ARCHITECT_ENABLED", False),
            ),
            provider=str(
                _pick(architect, "provider", env, "SYNTRA_ARCHITECT_PROVIDER", "")
            )
            or None,
            model=str(_pick(architect, "model", env, "SYNTRA_ARCHITECT_MODEL", ""))
            or None,
            api_timeout_seconds=_float(
                "architect.api_timeout_seconds",
                architect.get("api_timeout_seconds", 120.0),
            ),
        ),
        codex=CodexConfig(
            executable=str(codex.get("executable", "codex")),
            execution_timeout_seconds=_float(
                "codex.execution_timeout_seconds",
                _pick(
                    codex,
                    "execution_timeout_seconds",
                    env,
                    "SYNTRA_CODEX_TIMEOUT_SECONDS",
                    3600.0,
                ),
            ),
            worker_identity=str(codex.get("worker_identity", "syntra-codex")),
        ),
        github=GitHubConfig(
            enabled=_bool(
                "github.enabled",
                _pick(github, "enabled", env, "SYNTRA_GITHUB_ENABLED", False),
            ),
            owner=str(_pick(github, "owner", env, "SYNTRA_GITHUB_OWNER", "")) or None,
            api_timeout_seconds=_float(
                "github.api_timeout_seconds", github.get("api_timeout_seconds", 30.0)
            ),
        ),
        scheduler=SchedulerConfig(
            architect_concurrency=_int(
                "scheduler.architect_concurrency",
                _pick(
                    scheduler,
                    "architect_concurrency",
                    env,
                    "SYNTRA_ARCHITECT_CONCURRENCY",
                    2,
                ),
            ),
            codex_concurrency=_int(
                "scheduler.codex_concurrency",
                _pick(
                    scheduler, "codex_concurrency", env, "SYNTRA_CODEX_CONCURRENCY", 2
                ),
            ),
            repository_provisioning_concurrency=_int(
                "scheduler.repository_provisioning_concurrency",
                _pick(
                    scheduler,
                    "repository_provisioning_concurrency",
                    env,
                    "SYNTRA_REPOSITORY_PROVISIONING_CONCURRENCY",
                    1,
                ),
            ),
            merge_concurrency=_int(
                "scheduler.merge_concurrency",
                _pick(
                    scheduler, "merge_concurrency", env, "SYNTRA_MERGE_CONCURRENCY", 1
                ),
            ),
        ),
        retries=RetryConfig(
            infrastructure_attempts=_int(
                "retries.infrastructure_attempts",
                _pick(
                    retries,
                    "infrastructure_attempts",
                    env,
                    "SYNTRA_INFRASTRUCTURE_RETRY_ATTEMPTS",
                    4,
                ),
            ),
            initial_backoff_seconds=_float(
                "retries.initial_backoff_seconds",
                retries.get("initial_backoff_seconds", 5.0),
            ),
            backoff_multiplier=_float(
                "retries.backoff_multiplier", retries.get("backoff_multiplier", 2.0)
            ),
            maximum_backoff_seconds=_float(
                "retries.maximum_backoff_seconds",
                retries.get("maximum_backoff_seconds", 600.0),
            ),
        ),
        logging=LoggingConfig(
            level=str(logging.get("level", "INFO")),
            structured=_bool("logging.structured", logging.get("structured", True)),
        ),
        metrics=MetricsConfig(
            enabled=_bool("metrics.enabled", metrics.get("enabled", True)),
            bind_host=str(metrics.get("bind_host", "127.0.0.1")),
            port=_int("metrics.port", metrics.get("port", 9464)),
        ),
        backups=BackupConfig(
            enabled=_bool("backups.enabled", backups.get("enabled", True)),
            retention_days=_int(
                "backups.retention_days", backups.get("retention_days", 30)
            ),
        ),
        security=ResourceThresholdConfig(
            disk_warning_percent_free=_float(
                "security.disk_warning_percent_free",
                security.get("disk_warning_percent_free", 20.0),
            ),
            disk_stop_codex_percent_free=_float(
                "security.disk_stop_codex_percent_free",
                security.get("disk_stop_codex_percent_free", 10.0),
            ),
            disk_critical_percent_free=_float(
                "security.disk_critical_percent_free",
                security.get("disk_critical_percent_free", 5.0),
            ),
        ),
        secrets=loaded_secrets,
    )
