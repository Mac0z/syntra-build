# ruff: noqa: E501
from __future__ import annotations

import json
from pathlib import Path

import pytest

from syntra_build.admin import main
from syntra_build.infrastructure.config import load_config
from syntra_build.infrastructure.persistence import bootstrap_database


def _host(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    for directory in (data, tmp_path / "app", tmp_path / "etc", tmp_path / "log"):
        directory.mkdir(parents=True, exist_ok=True)
    raw = {
        "filesystem": {
            "application_root": str(tmp_path / "app"),
            "configuration_root": str(tmp_path / "etc"),
            "data_root": str(data),
            "log_root": str(tmp_path / "log"),
            "workspace_root": str(data / "work"),
            "backup_root": str(data / "backups"),
        },
        "database": {"sqlite_path": str(data / "syntra.db")},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    config = load_config(raw, environ={})
    with bootstrap_database(config) as connection:
        connection.execute(
            "INSERT INTO projects(id,name,state,activity,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "00000000-0000-0000-0000-000000000001",
                "Example",
                "NEW",
                "Ready",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "example",
                "public",
            ),
        )
    return path, config.database.sqlite_path


def test_read_commands_are_non_mutating(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, database = _host(tmp_path)
    import sqlite3

    def snapshot() -> tuple[tuple[object, ...], ...]:
        with sqlite3.connect(database) as connection:
            return tuple(
                tuple(row)
                for table in (
                    "schema_migrations",
                    "projects",
                    "milestones",
                    "jobs",
                    "state_transitions",
                )
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            )

    before = snapshot()
    for arguments in (
        ["version"],
        ["health"],
        ["projects"],
        ["projects", "--active"],
        ["projects", "--waiting"],
        ["status", "Example"],
        ["integrity"],
        ["reconcile"],
    ):
        assert main(["--config", str(config), *arguments]) == 0
    assert snapshot() == before
    assert main(["--config", str(config), "status", "missing"]) == 2
    output = capsys.readouterr()
    assert "token" not in (output.out + output.err).casefold()


def test_backup_and_restore_verify_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _database = _host(tmp_path)
    assert main(["--config", str(config), "backup"]) == 0
    output = capsys.readouterr().out
    backup = Path(output.split("path=", 1)[1].split(" schema=", 1)[0])
    assert main(["--config", str(config), "restore-verify", str(backup)]) == 0
    assert (
        main(["--config", str(config), "restore-verify", str(tmp_path / "missing")])
        == 1
    )
