"""Regressions for installed-runtime module initialization."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_m14_runtime_import_path_succeeds_in_fresh_interpreter() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import syntra_build.application.projects",
                    "import syntra_build.adapters.github.repository_names",
                    "import syntra_build.smoke",
                )
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_smoke_module_entry_reaches_normal_configuration_handling(
    tmp_path: Path,
) -> None:
    missing_config = tmp_path / "missing-config.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "syntra_build.smoke",
            "local",
            "--config",
            str(missing_config),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr == "Syntra Build development smoke: FAIL\n"
    assert "ImportError" not in result.stderr
    assert "Traceback" not in result.stderr
