from pathlib import Path

from tests.integration.test_m30_service import _config

from syntra_build.application.lifecycle import JobTypeDispatcher
from syntra_build.domain import WorkerClass
from syntra_build.infrastructure.codex_runner import (
    DirectProcessLauncher,
    SudoCodexLauncher,
)
from syntra_build.service import ServiceRuntime


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
    assert executors[WorkerClass.ARCHITECT].job_types == {
        "ARCHITECT_DESIGN",
        "SPECIFICATION_DRAFT",
        "ARCHITECT_TASK",
        "ARCHITECT_REVIEW",
    }
    assert "CI_RECONCILE" in executors[WorkerClass.CI].job_types
    assert isinstance(runtime.codex_launcher, SudoCodexLauncher)
    assert not isinstance(runtime.codex_launcher, DirectProcessLauncher)
