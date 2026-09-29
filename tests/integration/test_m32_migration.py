# ruff: noqa: E501
from pathlib import Path

from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
    open_database,
)


def test_schema_26_to_27_preserves_evidence_and_accepts_task(tmp_path: Path) -> None:
    db = open_database(tmp_path / "m32.db")
    apply_migrations(db, MIGRATIONS[:26])
    db.execute(
        "INSERT INTO projects(id,name,state,created_at,updated_at,last_state_change_at,canonical_name,repository_visibility) VALUES('00000000-0000-0000-0000-000000000001','p','DESIGNING','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','p','public')"
    )
    db.execute(
        "INSERT INTO architect_requests(id,project_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,status) VALUES('old','00000000-0000-0000-0000-000000000001','DESIGN','fake','m','high','1.0','{}','old','2026-01-01T00:00:00+00:00','STARTED')"
    )
    apply_migrations(db)
    assert current_schema_version(db) == 27
    assert (
        db.execute(
            "SELECT request_type FROM architect_requests WHERE id='old'"
        ).fetchone()[0]
        == "DESIGN"
    )
    db.execute(
        "INSERT INTO architect_requests(id,project_id,request_type,provider,model,reasoning_level,request_schema_version,request_payload_json,correlation_id,started_at,status) VALUES('task','00000000-0000-0000-0000-000000000001','TASK','fake','m','high','1.0','{}','task','2026-01-01T00:00:00+00:00','STARTED')"
    )
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()


def test_fresh_schema_is_27(tmp_path: Path) -> None:
    db = open_database(tmp_path / "fresh.db")
    assert apply_migrations(db) == 27
    db.close()
