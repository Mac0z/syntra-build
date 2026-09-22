"""Typed, validated application configuration."""

from syntra_build.infrastructure.config.host import (
    DEFAULT_ARCHITECT_API_KEY_PATH,
    DEFAULT_GITHUB_TOKEN_PATH,
    DEFAULT_HOST_CONFIG_PATH,
    DEFAULT_TELEGRAM_TOKEN_PATH,
    load_host_config,
)
from syntra_build.infrastructure.config.loader import (
    load_config,
    read_protected_secret_file,
)
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

__all__ = [
    "ApplicationConfig",
    "DEFAULT_ARCHITECT_API_KEY_PATH",
    "DEFAULT_GITHUB_TOKEN_PATH",
    "DEFAULT_HOST_CONFIG_PATH",
    "DEFAULT_TELEGRAM_TOKEN_PATH",
    "ArchitectConfig",
    "BackupConfig",
    "CodexConfig",
    "ConfigurationError",
    "DatabaseConfig",
    "FilesystemConfig",
    "GitHubConfig",
    "LoggingConfig",
    "MetricsConfig",
    "ResourceThresholdConfig",
    "RetryConfig",
    "SchedulerConfig",
    "SecretInputs",
    "SecretValue",
    "TelegramConfig",
    "load_config",
    "load_host_config",
    "read_protected_secret_file",
]
