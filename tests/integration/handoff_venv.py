"""Offline, real virtual environments shared by host and recovery regressions."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


def populated_venv(workspace: Path, *, extra_files: int = 0) -> tuple[Path, int]:
    environment = workspace / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--symlinks", str(environment)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    site = next((environment / "lib").glob("python*/site-packages"))
    # Copy installed test dependencies offline; pip itself comes from ensurepip.
    for name in ("pytest", "_pytest", "packaging", "pluggy", "pygments"):
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.submodule_search_locations
        source = Path(next(iter(spec.submodule_search_locations)))
        shutil.copytree(source, site / name, symlinks=True)
    for index in range(extra_files):
        package = site / f"benchmark_dependency_{index // 100:03}"
        package.mkdir(exist_ok=True)
        (package / f"module_{index:05}.py").write_text(
            "VALUE = 'offline dependency-size fixture'\n" * 8
        )
    # Python 3.14 creates this itself; keep it explicit for the Git warning check.
    (environment / ".gitignore").write_text("*\n")
    (workspace / ".gitignore").write_text(".venv/\n")
    interpreter = environment / "bin/python"
    assert interpreter.is_symlink()
    assert any(
        path.is_symlink() and path.readlink().is_absolute()
        for path in (environment / "bin").glob("python*")
    )
    return environment, sum(1 for _ in environment.rglob("*"))
