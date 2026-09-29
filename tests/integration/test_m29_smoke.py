from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from syntra_build.infrastructure.persistence import apply_migrations, open_database


def test_m29_probe_loads_real_host_configuration(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "state.sqlite3"
    with open_database(database) as connection:
        assert apply_migrations(connection) == 26
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "filesystem": {
                    "application_root": str(tmp_path / "app"),
                    "configuration_root": str(tmp_path / "config"),
                    "data_root": str(data),
                    "log_root": str(tmp_path / "logs"),
                    "workspace_root": str(data / "workspaces"),
                    "backup_root": str(data / "backups"),
                },
                "database": {"sqlite_path": str(database)},
                "metrics": {"enabled": True, "bind_host": "127.0.0.1", "port": 9464},
                "security": {
                    "disk_warning_percent_free": 20,
                    "disk_stop_codex_percent_free": 10,
                    "disk_critical_percent_free": 5,
                },
            }
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path.cwd() / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "syntra_build.m29_smoke",
            "--config",
            str(config),
            "--telegram-token",
            str(tmp_path / "missing-telegram"),
            "--github-token",
            str(tmp_path / "missing-github"),
            "--architect-key",
            str(tmp_path / "missing-architect"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "M29 probe passed: /health /ready /metrics\n"
