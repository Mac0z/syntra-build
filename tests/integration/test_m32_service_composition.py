from pathlib import Path
from typing import cast

from syntra_build.application.lifecycle import JobTypeDispatcher
from syntra_build.domain import WorkerClass
from syntra_build.infrastructure.codex_runner import (
    DirectProcessLauncher,
    SudoCodexLauncher,
)
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


def test_production_m32_composition_is_nonempty_and_fail_closed(tmp_path: Path) -> None:
    runtime = ServiceRuntime(
        config_loader=lambda _path: _config(tmp_path),
        configure_runtime_logging=False,
    )
    executors = runtime._production_executors()
    assert set(executors) == {
        WorkerClass.ARCHITECT,
        WorkerClass.CODEX,
        WorkerClass.GIT,
        WorkerClass.GITHUB,
        WorkerClass.CI,
    }
    assert all(isinstance(item, JobTypeDispatcher) for item in executors.values())
    architect = cast(JobTypeDispatcher, executors[WorkerClass.ARCHITECT])
    ci = cast(JobTypeDispatcher, executors[WorkerClass.CI])
    assert architect.job_types == {
        "ARCHITECT_DESIGN",
        "SPECIFICATION_DRAFT",
        "ARCHITECT_TASK",
        "ARCHITECT_REVIEW",
    }
    assert "CI_RECONCILE" in ci.job_types
    assert isinstance(runtime.codex_launcher, SudoCodexLauncher)
    assert not isinstance(runtime.codex_launcher, DirectProcessLauncher)
