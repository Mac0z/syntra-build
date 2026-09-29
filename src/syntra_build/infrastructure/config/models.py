"""Configuration models and validation with no external side effects."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import ClassVar

from syntra_build.domain.jobs import WorkerClass


class ConfigurationError(ValueError):
    """Raised when application configuration is unsafe or malformed."""


def _positive(name: str, value: int | float) -> None:
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")


def _canonical_absolute(name: str, value: Path) -> Path:
    if not value.is_absolute():
        raise ConfigurationError(f"{name} must be an absolute path: {value}")
    return value.resolve(strict=False)


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


@dataclass(frozen=True, slots=True)
class SecretValue:
    """A secret input whose representation never includes its value."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.value:
            raise ConfigurationError("secret values must not be empty")

    def __repr__(self) -> str:
        return "SecretValue('[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"


@dataclass(frozen=True, slots=True)
class SecretInputs:
    """Runtime credentials, deliberately separate from ordinary settings."""

    telegram_bot_token: SecretValue | None = field(default=None, repr=False)
    architect_api_key: SecretValue | None = field(default=None, repr=False)
    codex_provider_credential: SecretValue | None = field(default=None, repr=False)
    github_token: SecretValue | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        present = [
            name
            for name in (
                "telegram_bot_token",
                "architect_api_key",
                "codex_provider_credential",
                "github_token",
            )
            if getattr(self, name) is not None
        ]
        return f"SecretInputs(present={present!r}, values='[REDACTED]')"


@dataclass(frozen=True, slots=True)
class FilesystemConfig:
    application_root: Path = Path("/opt/syntra-build")
    configuration_root: Path = Path("/etc/syntra-build")
    data_root: Path = Path("/var/lib/syntra-build")
    log_root: Path = Path("/var/log/syntra-build")
    workspace_root: Path = Path("/var/lib/syntra-build/workspaces")
    backup_root: Path = Path("/var/lib/syntra-build/backups")

    def __post_init__(self) -> None:
        names = (
            "application_root",
            "configuration_root",
            "data_root",
            "log_root",
            "workspace_root",
            "backup_root",
        )
        for name in names:
            object.__setattr__(
                self, name, _canonical_absolute(name, getattr(self, name))
            )

        protected = {
            "application_root": self.application_root,
            "configuration_root": self.configuration_root,
            "log_root": self.log_root,
        }
        managed = {
            "data_root": self.data_root,
            "workspace_root": self.workspace_root,
            "backup_root": self.backup_root,
        }
        protected_items = tuple(protected.items())
        for index, (left_name, left_path) in enumerate(protected_items):
            for right_name, right_path in protected_items[index + 1 :]:
                if _contains(left_path, right_path) or _contains(right_path, left_path):
                    raise ConfigurationError(
                        f"{left_name} and {right_name} must not overlap"
                    )
        for protected_name, protected_path in protected.items():
            for managed_name, managed_path in managed.items():
                if _contains(protected_path, managed_path) or _contains(
                    managed_path, protected_path
                ):
                    raise ConfigurationError(
                        f"{protected_name} and {managed_name} must not overlap"
                    )
        if _contains(self.workspace_root, self.backup_root) or _contains(
            self.backup_root, self.workspace_root
        ):
            raise ConfigurationError("workspace_root and backup_root must not overlap")
        if not _contains(self.data_root, self.workspace_root):
            raise ConfigurationError("workspace_root must be contained by data_root")
        if self.workspace_root == self.data_root:
            raise ConfigurationError("workspace_root must not equal data_root")
        if not _contains(self.data_root, self.backup_root):
            raise ConfigurationError("backup_root must be contained by data_root")


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    sqlite_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sqlite_path", _canonical_absolute("sqlite_path", self.sqlite_path)
        )


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    enabled: bool = False
    authorised_user_ids: tuple[int, ...] = ()
    polling_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        _positive("telegram.polling_timeout_seconds", self.polling_timeout_seconds)
        if any(
            type(user_id) is not int or user_id <= 0
            for user_id in self.authorised_user_ids
        ):
            raise ConfigurationError(
                "telegram.authorised_user_ids must contain positive integer IDs"
            )
        if len(set(self.authorised_user_ids)) != len(self.authorised_user_ids):
            raise ConfigurationError("telegram.authorised_user_ids must be unique")
        if self.enabled and not self.authorised_user_ids:
            raise ConfigurationError(
                "telegram.authorised_user_ids is required when Telegram is enabled"
            )


@dataclass(frozen=True, slots=True)
class ArchitectConfig:
    enabled: bool = False
    provider: str | None = None
    model: str | None = None
    api_timeout_seconds: float = 600.0
    reasoning_effort: str = "high"

    def __post_init__(self) -> None:
        _positive("architect.api_timeout_seconds", self.api_timeout_seconds)
        if self.reasoning_effort not in {"low", "medium", "high"}:
            raise ConfigurationError("architect.reasoning_effort is invalid")
        if self.enabled and (not self.provider or not self.model):
            raise ConfigurationError(
                "architect.provider and architect.model are required when enabled"
            )


@dataclass(frozen=True, slots=True)
class CodexConfig:
    executable: str = "codex"
    execution_timeout_seconds: float = 3600.0
    worker_identity: str = "syntra-codex"

    def __post_init__(self) -> None:
        _positive("codex.execution_timeout_seconds", self.execution_timeout_seconds)
        if not self.executable.strip():
            raise ConfigurationError("codex.executable must not be empty")
        if not self.worker_identity.strip():
            raise ConfigurationError("codex.worker_identity must not be empty")


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    enabled: bool = False
    owner: str | None = None
    api_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        _positive("github.api_timeout_seconds", self.api_timeout_seconds)
        if self.enabled and not self.owner:
            raise ConfigurationError("github.owner is required when GitHub is enabled")


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    architect_concurrency: int = 2
    codex_concurrency: int = 2
    repository_provisioning_concurrency: int = 1
    merge_concurrency: int = 1
    ci_concurrency: int = 8
    messaging_concurrency: int = 8
    recovery_concurrency: int = 2
    internal_concurrency: int = 4

    MAX_CONCURRENCY: ClassVar[int] = 128

    def __post_init__(self) -> None:
        for name in (
            "architect_concurrency",
            "codex_concurrency",
            "repository_provisioning_concurrency",
            "merge_concurrency",
            "ci_concurrency",
            "messaging_concurrency",
            "recovery_concurrency",
            "internal_concurrency",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= self.MAX_CONCURRENCY:
                raise ConfigurationError(
                    f"scheduler.{name} must be between 1 and {self.MAX_CONCURRENCY}"
                )

    def worker_class_limits(self) -> dict[WorkerClass, int]:
        """Map M1 operation names onto M9 worker-class capacity."""
        return {
            WorkerClass.ARCHITECT: self.architect_concurrency,
            WorkerClass.CODEX: self.codex_concurrency,
            WorkerClass.GIT: self.repository_provisioning_concurrency,
            WorkerClass.GITHUB: self.merge_concurrency,
            WorkerClass.CI: self.ci_concurrency,
            WorkerClass.MESSAGING: self.messaging_concurrency,
            WorkerClass.RECOVERY: self.recovery_concurrency,
            WorkerClass.INTERNAL: self.internal_concurrency,
        }


@dataclass(frozen=True, slots=True)
class RetryConfig:
    infrastructure_attempts: int = 4
    backoff_schedule_seconds: tuple[float, ...] = (5.0, 30.0, 120.0, 600.0)
    jitter_factor: float = 0.2
    codex_cycle_limit: int = 5
    ci_rework_limit: int = 5
    architect_rework_limit: int = 5
    human_test_rework_limit: int = 5

    def __post_init__(self) -> None:
        if (
            type(self.infrastructure_attempts) is not int
            or not 1 <= self.infrastructure_attempts <= 100
        ):
            raise ConfigurationError(
                "retries.infrastructure_attempts must be between 1 and 100"
            )
        if not self.backoff_schedule_seconds or any(
            value < 0 for value in self.backoff_schedule_seconds
        ):
            raise ConfigurationError("retries.backoff_schedule_seconds is invalid")
        if any(
            left > right
            for left, right in zip(
                self.backoff_schedule_seconds, self.backoff_schedule_seconds[1:]
            )
        ):
            raise ConfigurationError(
                "retries.backoff_schedule_seconds must not decrease"
            )
        if not 0 <= self.jitter_factor <= 1:
            raise ConfigurationError("retries.jitter_factor must be between 0 and 1")
        for name in (
            "codex_cycle_limit",
            "ci_rework_limit",
            "architect_rework_limit",
            "human_test_rework_limit",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ConfigurationError(f"retries.{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    structured: bool = True

    def __post_init__(self) -> None:
        normalised = self.level.upper()
        if normalised not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("logging.level is invalid")
        object.__setattr__(self, "level", normalised)


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    port: int = 9464

    def __post_init__(self) -> None:
        if not self.bind_host:
            raise ConfigurationError("metrics.bind_host must not be empty")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ConfigurationError("metrics.port must be between 1 and 65535")


@dataclass(frozen=True, slots=True)
class BackupConfig:
    enabled: bool = True
    retention_days: int = 30

    def __post_init__(self) -> None:
        if type(self.retention_days) is not int or self.retention_days <= 0:
            raise ConfigurationError(
                "backups.retention_days must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class ResourceThresholdConfig:
    disk_warning_percent_free: float = 20.0
    disk_stop_codex_percent_free: float = 10.0
    disk_critical_percent_free: float = 5.0

    def __post_init__(self) -> None:
        values = (
            self.disk_critical_percent_free,
            self.disk_stop_codex_percent_free,
            self.disk_warning_percent_free,
        )
        if not all(0 < value < 100 for value in values):
            raise ConfigurationError(
                "security disk thresholds must be between 0 and 100"
            )
        if not values[0] < values[1] < values[2]:
            raise ConfigurationError(
                "security disk thresholds must satisfy critical < stop Codex < warning"
            )


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    filesystem: FilesystemConfig
    database: DatabaseConfig
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    architect: ArchitectConfig = field(default_factory=ArchitectConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    retries: RetryConfig = field(default_factory=RetryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    backups: BackupConfig = field(default_factory=BackupConfig)
    security: ResourceThresholdConfig = field(default_factory=ResourceThresholdConfig)
    secrets: SecretInputs = field(default_factory=SecretInputs, repr=False)

    def __post_init__(self) -> None:
        if not _contains(self.filesystem.data_root, self.database.sqlite_path):
            raise ConfigurationError(
                "database.sqlite_path must be contained by data_root"
            )
        required_secrets = (
            (self.telegram.enabled, self.secrets.telegram_bot_token, "Telegram token"),
            (
                self.architect.enabled,
                self.secrets.architect_api_key,
                "Architect API key",
            ),
            (self.github.enabled, self.secrets.github_token, "GitHub token"),
        )
        for enabled, secret, label in required_secrets:
            if enabled and secret is None:
                raise ConfigurationError(
                    f"{label} is required when its integration is enabled"
                )

    def safe_dict(self) -> dict[str, object]:
        """Return ordinary settings only; secret names and values are excluded."""
        values = asdict(self)
        values.pop("secrets", None)
        return values
