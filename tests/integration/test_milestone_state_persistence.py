import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from syntra_build.domain import (
    Milestone,
    MilestoneId,
    MilestoneState,
    MilestoneTransitionRequest,
    Project,
    ProjectId,
    ProjectState,
)
from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    ActiveMilestoneConflictError,
    MilestoneDependencyError,
    MilestoneProjectMismatchError,
    PersistenceError,
    SQLiteMilestoneRepository,
    SQLiteProjectRepository,
    StaleMilestoneStateError,
    UnsatisfiedMilestoneDependenciesError,
    apply_migrations,
    current_schema_version,
    open_database,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
P1 = ProjectId.from_string("00000000-0000-0000-0000-000000000001")
P2 = ProjectId.from_string("00000000-0000-0000-0000-000000000002")
M1 = MilestoneId.from_string("00000000-0000-0000-0000-000000000011")
M2 = MilestoneId.from_string("00000000-0000-0000-0000-000000000012")
M3 = MilestoneId.from_string("00000000-0000-0000-0000-000000000013")


def setup(
    path: Path, ids: Callable[[], str] | None = None
) -> tuple[sqlite3.Connection, SQLiteMilestoneRepository, SQLiteProjectRepository]:
    db = open_database(path)
    apply_migrations(db)
    projects = SQLiteProjectRepository(db, lambda: "project-transition")
    projects.add(Project(P1, "one", ProjectState.NEW, NOW, NOW))
    projects.add(Project(P2, "two", ProjectState.NEW, NOW, NOW))
    counter = iter(f"transition-{n}" for n in range(100))
    repo = SQLiteMilestoneRepository(db, ids or (lambda: next(counter)))
    repo.add(Milestone(M1, P1, 1, "M1", "first", MilestoneState.PENDING, NOW, NOW))
    repo.add(Milestone(M2, P1, 2, "M2", "second", MilestoneState.PENDING, NOW, NOW))
    repo.add(Milestone(M3, P2, 1, "M1", "other", MilestoneState.PENDING, NOW, NOW))
    return db, repo, projects


def req(
    mid: MilestoneId,
    pid: ProjectId,
    source: MilestoneState,
    target: MilestoneState,
    n: int = 1,
) -> MilestoneTransitionRequest:
    return MilestoneTransitionRequest(
        mid,
        pid,
        source,
        target,
        "reason",
        "SYSTEM",
        "worker",
        "corr",
        NOW + timedelta(seconds=n),
        "event",
        {"attempt": n, "verified": True},
    )


def walk(
    repo: SQLiteMilestoneRepository,
    mid: MilestoneId,
    pid: ProjectId,
    states: list[MilestoneState],
) -> None:
    for n, (source, target) in enumerate(zip(states, states[1:]), 1):
        repo.apply_transition(req(mid, pid, source, target, n))


def test_happy_path_metadata_activity_history_and_completion(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "happy.db")
    path = [
        MilestoneState.PENDING,
        MilestoneState.READY,
        MilestoneState.PREPARING_TASK,
        MilestoneState.PREPARING_WORKSPACE,
        MilestoneState.CODING,
        MilestoneState.VALIDATING_CHANGES,
        MilestoneState.COMMITTING,
        MilestoneState.PUSHING,
        MilestoneState.PR_CREATING,
        MilestoneState.CI_RUNNING,
        MilestoneState.ARCHITECT_REVIEW,
        MilestoneState.MERGE_READY,
        MilestoneState.MERGING,
        MilestoneState.MERGE_VERIFY,
        MilestoneState.COMPLETE,
    ]
    walk(repo, M1, P1, path)
    result = repo.get(M1, P1)
    history = repo.transitions(M1, P1)
    assert result.state is MilestoneState.COMPLETE
    assert result.started_at == NOW + timedelta(seconds=1)
    assert result.completed_at == NOW + timedelta(seconds=len(path) - 1)
    assert len(history) == len(path) - 1 and history[0].created_at.tzinfo is UTC
    assert history[0].metadata == {"attempt": 1, "verified": True}
    changed = repo.update_activity(M1, P1, "archived", NOW + timedelta(minutes=1))
    assert changed.activity == "archived" and len(repo.transitions(M1, P1)) == len(
        history
    )
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM state_transitions")
    db.close()


@pytest.mark.parametrize(
    "path",
    [
        [
            MilestoneState.CI_RUNNING,
            MilestoneState.CI_REWORK,
            MilestoneState.CODING,
            MilestoneState.VALIDATING_CHANGES,
            MilestoneState.COMMITTING,
            MilestoneState.PUSHING,
            MilestoneState.CI_RUNNING,
        ],
        [
            MilestoneState.ARCHITECT_REVIEW,
            MilestoneState.REVIEW_REWORK,
            MilestoneState.CODING,
            MilestoneState.VALIDATING_CHANGES,
            MilestoneState.COMMITTING,
            MilestoneState.PUSHING,
            MilestoneState.CI_RUNNING,
            MilestoneState.ARCHITECT_REVIEW,
        ],
        [
            MilestoneState.ARCHITECT_REVIEW,
            MilestoneState.HUMAN_TEST,
            MilestoneState.MERGE_READY,
        ],
        [
            MilestoneState.ARCHITECT_REVIEW,
            MilestoneState.HUMAN_TEST,
            MilestoneState.REVIEW_REWORK,
        ],
    ],
)
def test_documented_branch_paths(tmp_path: Path, path: list[MilestoneState]) -> None:
    db, repo, _ = setup(tmp_path / f"{path[0]}.db")
    db.execute("UPDATE milestones SET state=? WHERE id=?", (path[0].value, str(M1)))
    walk(repo, M1, P1, path)
    assert repo.get(M1, P1).state is path[-1]
    db.close()


def test_blocked_recovery_persists_exact_target_and_clears_it(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "blocked.db")
    walk(
        repo,
        M1,
        P1,
        [MilestoneState.PENDING, MilestoneState.READY, MilestoneState.PREPARING_TASK],
    )
    blocked = repo.apply_transition(
        req(M1, P1, MilestoneState.PREPARING_TASK, MilestoneState.BLOCKED, 3)
    )
    assert blocked.resume_state is MilestoneState.PREPARING_TASK
    with pytest.raises(ValueError, match="persisted recovery target"):
        repo.apply_transition(
            req(M1, P1, MilestoneState.BLOCKED, MilestoneState.CODING, 4)
        )
    restored = repo.apply_transition(
        req(M1, P1, MilestoneState.BLOCKED, MilestoneState.PREPARING_TASK, 4)
    )
    assert restored.resume_state is None
    db.close()


def test_dependencies_activation_and_isolation(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "deps.db")
    assert repo.is_activation_eligible(M1, P1)
    repo.add_dependency(M2, M1)
    assert not repo.is_activation_eligible(M2, P1)
    with pytest.raises(UnsatisfiedMilestoneDependenciesError):
        repo.apply_transition(req(M2, P1, MilestoneState.PENDING, MilestoneState.READY))
    db.execute("UPDATE milestones SET state='COMPLETE' WHERE id=?", (str(M1),))
    assert repo.is_activation_eligible(M2, P1)
    with pytest.raises(MilestoneDependencyError):
        repo.add_dependency(M2, M1)
    with pytest.raises(MilestoneDependencyError, match="same project"):
        repo.add_dependency(M2, M3)
    for state in (MilestoneState.FAILED, MilestoneState.CANCELLED):
        db.execute("UPDATE milestones SET state=? WHERE id=?", (state.value, str(M1)))
        assert not repo.is_activation_eligible(M2, P1)
    with pytest.raises(MilestoneProjectMismatchError):
        repo.get(M1, P2)
    db.close()


def test_one_active_per_project_but_projects_are_independent(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "active.db")
    repo.apply_transition(req(M1, P1, MilestoneState.PENDING, MilestoneState.READY))
    with pytest.raises(ActiveMilestoneConflictError):
        repo.apply_transition(req(M2, P1, MilestoneState.PENDING, MilestoneState.READY))
    repo.apply_transition(req(M3, P2, MilestoneState.PENDING, MilestoneState.READY))
    db.execute("UPDATE milestones SET state='COMPLETE' WHERE id=?", (str(M1),))
    assert (
        repo.apply_transition(
            req(M2, P1, MilestoneState.PENDING, MilestoneState.READY)
        ).state
        is MilestoneState.READY
    )
    db.close()


def test_stale_and_history_failure_are_atomic(tmp_path: Path) -> None:
    db, repo, _ = setup(tmp_path / "atomic.db")
    repo.apply_transition(req(M1, P1, MilestoneState.PENDING, MilestoneState.READY))
    with pytest.raises(StaleMilestoneStateError):
        repo.apply_transition(
            req(M1, P1, MilestoneState.PENDING, MilestoneState.CANCELLED)
        )
    db.execute(
        """CREATE TRIGGER force_milestone_history_failure
           BEFORE INSERT ON state_transitions WHEN NEW.entity_type='MILESTONE'
           BEGIN SELECT RAISE(ABORT,'forced'); END"""
    )
    before = repo.get(M1, P1)
    with pytest.raises(PersistenceError):
        repo.apply_transition(
            req(M1, P1, MilestoneState.READY, MilestoneState.PREPARING_TASK, 2)
        )
    after = repo.get(M1, P1)
    assert (after.state, after.started_at, after.updated_at) == (
        before.state,
        before.started_at,
        before.updated_at,
    )
    assert len(repo.transitions(M1, P1)) == 1
    db.close()


def test_upgrade_from_m7_preserves_project_history_and_api(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    with open_database(path) as db:
        apply_migrations(db, MIGRATIONS[:2])
        projects = SQLiteProjectRepository(db, lambda: "old-history")
        projects.add(Project(P1, "old", ProjectState.NEW, NOW, NOW))
        from syntra_build.domain import ProjectTransitionRequest

        projects.apply_transition(
            ProjectTransitionRequest(
                P1,
                ProjectState.NEW,
                ProjectState.DESIGNING,
                "old",
                "SYSTEM",
                None,
                "corr",
                NOW,
            )
        )
        apply_migrations(db)
        assert current_schema_version(db) == len(MIGRATIONS)
        assert projects.get(P1).state is ProjectState.DESIGNING
        assert len(projects.transitions(P1)) == 1
