from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from syntra_build.infrastructure.config import ApplicationConfig, load_config
from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    SQLiteProviderCursorRepository,
    apply_migrations,
    bootstrap_database,
    open_database,
)


def _config(root: Path) -> ApplicationConfig:
    (root / "data").mkdir(exist_ok=True)
    return load_config(
        {
            "filesystem": {
                "application_root": root / "app",
                "configuration_root": root / "config",
                "data_root": root / "data",
                "log_root": root / "logs",
            },
            "database": {"sqlite_path": root / "data" / "syntra.db"},
        },
        environ={},
    )


def test_cursor_advancement_is_monotonic_and_equal_is_deterministic(
    tmp_path: Path,
) -> None:
    with bootstrap_database(_config(tmp_path)) as connection:
        cursors = SQLiteProviderCursorRepository(
            connection, lambda: datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert cursors.get("telegram") is None
        assert cursors.advance("telegram", 102) == 102
        updated_at = connection.execute(
            "SELECT updated_at FROM provider_cursors WHERE provider='telegram'"
        ).fetchone()[0]
        assert cursors.advance("telegram", 100) == 102
        assert cursors.advance("telegram", 102) == 102
        assert (
            connection.execute(
                "SELECT updated_at FROM provider_cursors WHERE provider='telegram'"
            ).fetchone()[0]
            == updated_at
        )


def test_m14_database_upgrade_preserves_project_and_adds_cursor(tmp_path: Path) -> None:
    path = tmp_path / "m14.db"
    with open_database(path) as connection:
        apply_migrations(connection, MIGRATIONS[:-1])
        connection.execute(
            """INSERT INTO projects
               (id,name,state,created_at,updated_at,last_state_change_at,canonical_name)
               VALUES
               ('project-1','M14 Acceptance','DESIGNING',?,?,?,'m14 acceptance')""",
            ("2026-01-01T00:00:00+00:00",) * 3,
        )
        apply_migrations(connection)
        row = connection.execute(
            "SELECT name,state FROM projects WHERE id='project-1'"
        ).fetchone()
        assert row is not None
        assert tuple(row) == ("M14 Acceptance", "DESIGNING")
        assert SQLiteProviderCursorRepository(connection).get("telegram") is None


def test_cursor_survives_a_fresh_python_process(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with bootstrap_database(config) as connection:
        SQLiteProviderCursorRepository(connection).advance("telegram", 102)
    code = f"""
from pathlib import Path
from syntra_build.infrastructure.persistence import (
    open_database, SQLiteProviderCursorRepository
)
with open_database(Path({str(config.database.sqlite_path)!r})) as db:
    print(SQLiteProviderCursorRepository(db).get('telegram'))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "102"
