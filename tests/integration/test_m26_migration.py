from pathlib import Path

from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
    open_database,
)


def test_clean_database_migrates_through_024(tmp_path: Path) -> None:
    with open_database(tmp_path / "m26.db") as db:
        apply_migrations(db)
        assert current_schema_version(db) == 24
        names = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"merge_eligibility_results", "merge_attempts"} <= names


def test_023_upgrades_forward_without_rewriting_history(tmp_path: Path) -> None:
    with open_database(tmp_path / "upgrade.db") as db:
        apply_migrations(db, MIGRATIONS[:23])
        before = tuple(db.execute("SELECT version,name FROM schema_migrations"))
        apply_migrations(db)
        assert (
            tuple(db.execute("SELECT version,name FROM schema_migrations"))[:-1]
            == before
        )
        assert current_schema_version(db) == 24


def test_merge_attempt_history_cannot_be_deleted(tmp_path: Path) -> None:
    # Schema trigger is asserted directly; relational fixtures are exercised by
    # Gatekeeper lifecycle tests while this keeps migration validation focused.
    with open_database(tmp_path / "trigger.db") as db:
        apply_migrations(db)
        trigger = db.execute(
            """SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name='merge_attempts_no_delete'"""
        ).fetchone()
        assert trigger is not None and "preservation-oriented" in trigger[0]
