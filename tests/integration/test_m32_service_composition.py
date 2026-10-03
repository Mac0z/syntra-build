from pathlib import Path
from types import SimpleNamespace

import pytest

from syntra_build.application.lifecycle import JobTypeDispatcher
from syntra_build.application.production import PRODUCTION_JOB_TYPES
from syntra_build.domain.jobs import WorkerClass
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.config.models import (
    ApplicationConfig,
    ConfigurationError,
    SecretInputs,
    SecretValue,
)
from syntra_build.service import ServiceRuntime


def _config(tmp_path: Path) -> ApplicationConfig:
    data = tmp_path / "data"
    for path in (data, tmp_path / "app", tmp_path / "etc", tmp_path / "log"):
        path.mkdir(parents=True, exist_ok=True)
    return load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "app"),
                "configuration_root": str(tmp_path / "etc"),
                "data_root": str(data),
                "log_root": str(tmp_path / "log"),
                "workspace_root": str(data / "workspaces"),
                "backup_root": str(data / "backups"),
            },
            "database": {"sqlite_path": str(data / "syntra.db")},
            "architect": {
                "enabled": True,
                "provider": "openai",
                "model": "test-model",
            },
            "codex": {"executable": "/bin/true"},
            "github": {"enabled": True, "owner": "Mac0z"},
            "metrics": {"enabled": False},
            "backups": {"enabled": False},
        },
        environ={},
        secrets=SecretInputs(
            architect_api_key=SecretValue("synthetic-architect-key"),
            github_token=SecretValue("synthetic-github-token"),
        ),
    )


def test_runtime_uses_exact_production_routes_by_default(
    tmp_path: Path,
) -> None:
    runtime = ServiceRuntime(
        config_loader=lambda _path: _config(tmp_path),
        configure_runtime_logging=False,
    )
    executors = runtime._production_executors()
    assert set(executors) == set(PRODUCTION_JOB_TYPES)
    for worker, expected in PRODUCTION_JOB_TYPES.items():
        dispatcher = executors[worker]
        assert isinstance(dispatcher, JobTypeDispatcher)
        assert dispatcher.job_types == expected
        with pytest.raises(RuntimeError, match="unsupported trusted job type"):
            dispatcher.execute(SimpleNamespace(job_type="UNRELEASED_HELPER"))  # type: ignore[arg-type]
    runtime.start()
    try:
        assert runtime.scheduler is not None
        assert runtime.scheduler.is_draining is False
    finally:
        runtime.stop()


def test_explicit_executor_factory_still_wins(tmp_path: Path) -> None:
    sentinel = object()
    runtime = ServiceRuntime(
        config_loader=lambda _path: _config(tmp_path),
        configure_runtime_logging=False,
        executor_factory=lambda _config: {WorkerClass.CI: sentinel},
    )
    assert runtime._production_executors() == {WorkerClass.CI: sentinel}


def test_production_default_fails_closed_without_required_providers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    disabled = ApplicationConfig(
        filesystem=config.filesystem,
        database=config.database,
        metrics=config.metrics,
        backups=config.backups,
    )
    runtime = ServiceRuntime(
        config_loader=lambda _path: disabled,
        configure_runtime_logging=False,
    )
    with pytest.raises(ConfigurationError, match="Architect"):
        runtime._production_executors()
