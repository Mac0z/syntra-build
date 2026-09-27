from pathlib import Path
from uuid import uuid4

import pytest
from test_m26_gatekeeper import NOW, SHA, seed_eligible

from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    apply_migrations,
    current_schema_version,
    open_database,
)


def test_clean_database_migrates_through_024(tmp_path: Path) -> None:
    with open_database(tmp_path / "m26.db") as db:
        apply_migrations(db, MIGRATIONS[:24])
        assert current_schema_version(db) == 24
        names = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"merge_eligibility_results", "merge_attempts"} <= names
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_genuine_023_upgrade_preserves_m25_data_and_builds_m26_constraints(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "upgrade.db") as db:
        apply_migrations(db, MIGRATIONS[:23])
        seed, _github = seed_eligible(db)
        tables = (
            "projects",
            "milestones",
            "github_repositories",
            "pull_requests",
            "ci_runs",
            "architect_reviews",
        )
        before = {
            name: tuple(
                tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")
            )
            for name in tables
        }
        existing = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "merge_eligibility_results" not in existing
        assert "merge_attempts" not in existing

        apply_migrations(db, MIGRATIONS[:24])

        after = {
            name: tuple(
                tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")
            )
            for name in tables
        }
        assert after == before
        assert current_schema_version(db) == 24
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        schema = {
            row[0]
            for row in db.execute(
                """SELECT name FROM sqlite_master
                WHERE type IN ('table','index','trigger')"""
            )
        }
        assert {
            "merge_eligibility_results",
            "merge_attempts",
            "one_global_active_merge",
            "merge_attempts_identity_immutable",
            "merge_attempts_no_delete",
            "merge_results_no_update",
            "merge_results_no_delete",
        } <= schema

        result_id, attempt_id = str(uuid4()), str(uuid4())
        db.execute(
            """INSERT INTO merge_eligibility_results VALUES
            (?,'1.0','corr',?,?,?,?,42,7,?,1,'[]','{}',?)""",
            (
                result_id,
                str(seed.project),
                str(seed.milestone),
                seed.repo,
                seed.pr,
                SHA,
                NOW.isoformat(),
            ),
        )
        payload = '{"interface_version":"1.0","eligible":true}'
        db.execute(
            """INSERT INTO merge_attempts
            (id,project_id,milestone_id,pull_request_id,expected_head_sha,
             expected_head_branch,expected_base_branch,
             gatekeeper_result_id,gatekeeper_result_json,merge_strategy,status,requested_at)
             VALUES (?,?,?,?,?,'feature','main',?,?,'SQUASH','REQUESTED',?)""",
            (
                attempt_id,
                str(seed.project),
                str(seed.milestone),
                seed.pr,
                SHA,
                result_id,
                payload,
                NOW.isoformat(),
            ),
        )
        with pytest.raises(Exception, match="UNIQUE constraint"):
            db.execute(
                """INSERT INTO merge_attempts
                (id,project_id,milestone_id,pull_request_id,expected_head_sha,
                 expected_head_branch,expected_base_branch,
                 gatekeeper_result_id,gatekeeper_result_json,merge_strategy,status,requested_at)
                 VALUES (?,?,?,?,?,'feature','main',?,?,'SQUASH','REQUESTED',?)""",
                (
                    str(uuid4()),
                    str(seed.project),
                    str(seed.milestone),
                    seed.pr,
                    SHA,
                    result_id,
                    payload,
                    NOW.isoformat(),
                ),
            )
        with pytest.raises(Exception, match="identity is immutable"):
            db.execute(
                "UPDATE merge_attempts SET expected_head_sha=? WHERE id=?",
                ("b" * 40, attempt_id),
            )
        with pytest.raises(Exception, match="preservation-oriented"):
            db.execute("DELETE FROM merge_attempts WHERE id=?", (attempt_id,))
        with pytest.raises(Exception, match="immutable"):
            db.execute(
                "UPDATE merge_eligibility_results SET eligible=0 WHERE id=?",
                (result_id,),
            )
        with pytest.raises(Exception, match="preservation-oriented"):
            db.execute("DELETE FROM merge_eligibility_results WHERE id=?", (result_id,))
