from pathlib import Path

from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.config.models import ApplicationConfig, SecretInputs
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
            "metrics": {"enabled": False},
            "backups": {"enabled": False},
        },
        environ={},
        secrets=SecretInputs(),
    )


def test_m32_foundation_does_not_enable_unfinished_production_routes(
    tmp_path: Path,
) -> None:
    runtime = ServiceRuntime(
        config_loader=lambda _path: _config(tmp_path),
        configure_runtime_logging=False,
    )
    assert runtime._production_executors() == {}
    runtime.start()
    try:
        assert runtime.scheduler is not None
        assert runtime.scheduler.is_draining is False
    finally:
        runtime.stop()
