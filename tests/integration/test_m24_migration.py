from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
)


def test_migration_020_to_021_preserves_architect_history(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    project_id, request_id, response_id = (str(uuid4()) for _ in range(3))
    with open_database(tmp_path / "upgrade.db") as connection:
        apply_migrations(connection, MIGRATIONS[:-1])
        assert current_schema_version(connection) == 20
        connection.execute(
            """INSERT INTO projects
            (id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility)
            VALUES (?,?,'DESIGNING',?,?,?,?, 'public')""",
            (project_id, "history", now, now, now, "history"),
        )
        connection.execute(
            """INSERT INTO architect_requests
            (id,project_id,request_type,provider,model,reasoning_level,
             request_schema_version,request_payload_json,correlation_id,started_at,status)
            VALUES (?,?,'DESIGN','fake','model','high','1.0','{}','old',
                   ?,'SUCCEEDED')""",
            (request_id, project_id, now),
        )
        connection.execute(
            """INSERT INTO architect_responses
            (id,architect_request_id,response_type,response_schema_version,
             normalised_payload_json,status,created_at,validation_status,provider,model)
            VALUES (?,?,'DESIGN','1.0','{}','ACCEPTED',?,'VALID','fake','model')""",
            (response_id, request_id, now),
        )
        apply_migrations(connection)
        assert current_schema_version(connection) == 21
        request = connection.execute(
            "SELECT * FROM architect_requests WHERE id=?", (request_id,)
        ).fetchone()
        response = connection.execute(
            "SELECT * FROM architect_responses WHERE id=?", (response_id,)
        ).fetchone()
        assert request is not None and request["request_type"] == "DESIGN"
        assert response is not None and response["response_type"] == "DESIGN"
        assert list(connection.execute("PRAGMA foreign_key_check")) == []


def test_clean_database_reaches_migration_021(tmp_path: Path) -> None:
    with open_database(tmp_path / "clean.db") as connection:
        apply_migrations(connection)
        assert current_schema_version(connection) == 21
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "architect_reviews",
            "architect_review_findings",
            "architect_rework_tasks",
        } <= tables
