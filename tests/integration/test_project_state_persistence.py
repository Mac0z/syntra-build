"""Integration coverage for atomic authoritative M7 project persistence."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from syntra_build.domain import (
    Project,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
)
from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    PersistenceError,
    SQLiteProjectRepository,
    StaleProjectStateError,
    apply_migrations,
    current_schema_version,
    open_database,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
PROJECT_ID = ProjectId.from_string("00000000-0000-0000-0000-000000000007")


def request(
    previous: ProjectState, target: ProjectState, when: datetime = NOW
) -> ProjectTransitionRequest:
    return ProjectTransitionRequest(
        PROJECT_ID,
        previous,
        target,
        "acceptance reason",
        "SYSTEM",
        "worker-1",
        "correlation-7",
        when,
        "event-7",
    )


def repository(
    path: Path, id_factory: Callable[[], str] = lambda: "transition-1"
) -> tuple[sqlite3.Connection, SQLiteProjectRepository]:
    db = open_database(path)
    apply_migrations(db)
    repo = SQLiteProjectRepository(db, id_factory)
    repo.add(Project(PROJECT_ID, "M7 project", ProjectState.NEW, NOW, NOW))
    return db, repo


def test_clean_schema_and_upgrade_from_pre_m7(tmp_path: Path) -> None:
    with open_database(tmp_path / "upgrade.db") as db:
        apply_migrations(db, MIGRATIONS[:1])
        assert current_schema_version(db) == 1
        apply_migrations(db)
        assert current_schema_version(db) == len(MIGRATIONS)
        project_columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
        assert {
            "state",
            "resume_state",
            "activity",
            "updated_at",
            "last_state_change_at",
        } <= project_columns
        transition_columns = {
            row[1] for row in db.execute("PRAGMA table_info(state_transitions)")
        }
        assert {
            "previous_state",
            "new_state",
            "reason",
            "actor_type",
            "correlation_id",
        } <= transition_columns


def test_transition_persists_state_history_metadata_and_utc(tmp_path: Path) -> None:
    db, repo = repository(tmp_path / "state.db")
    with db:
        changed = repo.apply_transition(
            request(ProjectState.NEW, ProjectState.DESIGNING)
        )
        history = repo.transitions(PROJECT_ID)
        assert changed.state is ProjectState.DESIGNING
        assert changed.updated_at == NOW == changed.last_state_change_at
        assert len(history) == 1
        assert (history[0].previous_state, history[0].new_state) == (
            ProjectState.NEW,
            ProjectState.DESIGNING,
        )
        assert (history[0].reason, history[0].actor_type, history[0].actor_id) == (
            "acceptance reason",
            "SYSTEM",
            "worker-1",
        )
        assert history[0].correlation_id == "correlation-7"
        assert history[0].trigger_event_id == "event-7"
        assert history[0].created_at.tzinfo is UTC


def test_pause_resume_and_activity_are_independent(tmp_path: Path) -> None:
    ids = iter(("transition-1", "transition-2", "transition-3"))
    db, repo = repository(tmp_path / "pause.db", lambda: next(ids))
    repo.apply_transition(request(ProjectState.NEW, ProjectState.DESIGNING))
    paused = repo.apply_transition(
        request(ProjectState.DESIGNING, ProjectState.PAUSED, NOW + timedelta(seconds=1))
    )
    assert paused.resume_state is ProjectState.DESIGNING
    active = repo.update_activity(
        PROJECT_ID, "Waiting for operator", NOW + timedelta(seconds=2)
    )
    assert active.state is ProjectState.PAUSED
    assert active.activity == "Waiting for operator"
    assert len(repo.transitions(PROJECT_ID)) == 2
    resumed = repo.apply_transition(
        request(ProjectState.PAUSED, ProjectState.DESIGNING, NOW + timedelta(seconds=3))
    )
    assert resumed.resume_state is None
    assert len(repo.transitions(PROJECT_ID)) == 3
    db.close()


def test_arbitrary_or_missing_resume_and_terminal_pause_fail(tmp_path: Path) -> None:
    ids = iter(("transition-1", "transition-2", "transition-3"))
    db, repo = repository(tmp_path / "guards.db", lambda: next(ids))
    repo.apply_transition(request(ProjectState.NEW, ProjectState.DESIGNING))
    repo.apply_transition(request(ProjectState.DESIGNING, ProjectState.PAUSED))
    with pytest.raises(ValueError, match="persisted"):
        repo.apply_transition(
            request(ProjectState.PAUSED, ProjectState.DESIGN_APPROVAL)
        )
    db.execute("UPDATE projects SET resume_state=NULL")
    with pytest.raises(ValueError, match="no valid"):
        repo.apply_transition(request(ProjectState.PAUSED, ProjectState.DESIGNING))
    db.execute("UPDATE projects SET state='COMPLETE'")
    with pytest.raises(ValueError):
        repo.apply_transition(request(ProjectState.COMPLETE, ProjectState.PAUSED))
    db.close()


def test_history_failure_rolls_back_state_change(tmp_path: Path) -> None:
    db, repo = repository(tmp_path / "rollback.db")
    db.execute(
        """CREATE TRIGGER force_history_failure BEFORE INSERT ON state_transitions
           BEGIN SELECT RAISE(ABORT, 'forced history failure'); END"""
    )
    with pytest.raises(PersistenceError):
        repo.apply_transition(request(ProjectState.NEW, ProjectState.DESIGNING))
    assert repo.get(PROJECT_ID).state is ProjectState.NEW
    assert repo.transitions(PROJECT_ID) == ()
    db.close()


def test_stale_expected_state_cannot_overwrite_newer_state(tmp_path: Path) -> None:
    ids = iter(("transition-1", "transition-2"))
    db, repo = repository(tmp_path / "stale.db", lambda: next(ids))
    repo.apply_transition(request(ProjectState.NEW, ProjectState.DESIGNING))
    with pytest.raises(StaleProjectStateError):
        repo.apply_transition(request(ProjectState.NEW, ProjectState.CANCELLED))
    assert repo.get(PROJECT_ID).state is ProjectState.DESIGNING
    assert len(repo.transitions(PROJECT_ID)) == 1
    db.close()


def test_transition_history_is_database_enforced_append_only(tmp_path: Path) -> None:
    db, repo = repository(tmp_path / "immutable.db")
    repo.apply_transition(request(ProjectState.NEW, ProjectState.DESIGNING))
    with pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE state_transitions SET reason='rewritten'")
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM state_transitions")
    db.close()
