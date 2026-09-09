# ruff: noqa: E501
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from syntra_build.application.commands import Command, CommandParser, InboundMessage
from syntra_build.application.projects import (
    ProjectCreationError,
    ProjectCreationFailure,
    ProjectCreationService,
    SQLiteProjectQueryService,
)
from syntra_build.domain import Project, ProjectId, ProjectState, WorkflowEventId
from syntra_build.infrastructure.persistence import (
    SQLiteProjectRepository,
    SQLiteWorkflowEventRepository,
    apply_migrations,
    open_database,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
PROJECT_ID = ProjectId.from_string("00000000-0000-0000-0000-000000000014")
EVENT_ID = WorkflowEventId.from_string("00000000-0000-0000-0000-000000000114")


@dataclass
class FakeGitHubNames:
    conflict: bool = False
    failure: bool = False
    calls: int = 0

    def conflicts(self, canonical_name: str) -> bool:
        self.calls += 1
        assert canonical_name == "flow-track"
        if self.failure:
            raise TimeoutError("synthetic outage")
        return self.conflict


def command(update: str = "10", name: str = "Flow Track") -> Command:
    parsed = CommandParser(lambda: "correlation-14").parse(
        InboundMessage(
            "telegram",
            update,
            "20",
            "30",
            NOW,
            f"create {name} | Build a desktop tracker exactly",
            "40",
            "50",
        )
    )
    assert parsed.command is not None
    return parsed.command


def services(
    path: Path, checker: FakeGitHubNames
) -> tuple[
    sqlite3.Connection,
    SQLiteProjectRepository,
    SQLiteWorkflowEventRepository,
    ProjectCreationService,
]:
    db = open_database(path)
    apply_migrations(db)
    projects = SQLiteProjectRepository(db, lambda: "transition-14")
    events = SQLiteWorkflowEventRepository(db)
    service = ProjectCreationService(
        db,
        projects,
        events,
        checker,
        project_id_factory=lambda: PROJECT_ID,
        event_id_factory=lambda: EVENT_ID,
    )
    return db, projects, events, service


def test_creation_is_atomic_idempotent_queryable_and_restart_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "m14.db"
    db, projects, events, service = services(path, FakeGitHubNames())
    first = service.create_from_command(command())
    second = service.create_from_command(command())
    assert first.project.state is ProjectState.DESIGNING
    assert second.duplicate and second.project.id == PROJECT_ID
    assert len(projects.list_all()) == len(events.for_project(PROJECT_ID)) == 1
    assert [
        (item.previous_state, item.new_state)
        for item in projects.transitions(PROJECT_ID)
    ] == [(ProjectState.NEW, ProjectState.DESIGNING)]
    query = SQLiteProjectQueryService(projects)
    assert (
        query.resolve_project("Flow Track").project
        == query.resolve_project(str(PROJECT_ID)).project
    )
    assert query.list_projects()[0].state is ProjectState.DESIGNING
    db.close()

    reopened = open_database(path)
    apply_migrations(reopened)
    repo = SQLiteProjectRepository(reopened, lambda: "unused")
    saved, context = repo.get(PROJECT_ID), repo.get_creation_context(PROJECT_ID)
    assert (saved.name, saved.canonical_name, saved.state) == (
        "Flow Track",
        "flow-track",
        ProjectState.DESIGNING,
    )
    assert (context.owner_id, context.initial_request, context.messaging_platform) == (
        "30",
        "Build a desktop tracker exactly",
        "telegram",
    )
    assert (context.conversation_id, context.thread_id) == ("40", "50")
    assert len(repo.transitions(PROJECT_ID)) == 1
    reopened.close()


@pytest.mark.parametrize(
    ("checker", "failure"),
    [
        (FakeGitHubNames(conflict=True), ProjectCreationFailure.GITHUB_CONFLICT),
        (FakeGitHubNames(failure=True), ProjectCreationFailure.GITHUB_UNAVAILABLE),
    ],
)
def test_github_conflict_checks_fail_closed(
    tmp_path: Path, checker: FakeGitHubNames, failure: ProjectCreationFailure
) -> None:
    db, projects, _, service = services(tmp_path / f"{failure}.db", checker)
    with pytest.raises(ProjectCreationError) as raised:
        service.create_from_command(command())
    assert raised.value.failure is failure
    assert projects.list_all() == ()
    db.close()


def test_canonical_conflicts_and_database_constraint(tmp_path: Path) -> None:
    db, projects, _, service = services(tmp_path / "conflict.db", FakeGitHubNames())
    service.create_from_command(command())
    with pytest.raises(ProjectCreationError) as raised:
        service.create_from_command(command("11", "FLOW TRACK"))
    assert raised.value.failure is ProjectCreationFailure.LOCAL_CONFLICT
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO projects(id,name,state,created_at,updated_at,last_state_change_at,canonical_name) VALUES(?,?,?,?,?,?,?)",
            (
                "00000000-0000-0000-0000-000000000099",
                "other",
                "NEW",
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                "flow-track",
            ),
        )
    db.close()


def test_failure_mid_creation_rolls_everything_back(tmp_path: Path) -> None:
    db, projects, _, service = services(tmp_path / "rollback.db", FakeGitHubNames())
    db.execute(
        "CREATE TRIGGER break_context BEFORE INSERT ON project_creation_context BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
    )
    with pytest.raises(ProjectCreationError) as raised:
        service.create_from_command(command())
    assert raised.value.failure is ProjectCreationFailure.PERSISTENCE
    assert projects.list_all() == ()
    assert db.execute("SELECT count(*) FROM workflow_events").fetchone()[0] == 0
    db.close()


def test_external_deduplication_constraint_race_returns_committed_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dedup-race.db"
    winner_db, winner_projects, winner_events, winner = services(
        path, FakeGitHubNames()
    )
    loser_db = open_database(path)
    apply_migrations(loser_db)

    class RacingProjects(SQLiteProjectRepository):
        def add(self, project: Project) -> None:
            committed = winner.create_from_command(command())
            assert committed.project.id == PROJECT_ID
            raise PersistenceError("synthetic lost concurrent insertion")

    loser_projects = RacingProjects(loser_db, lambda: "loser-transition")
    loser = ProjectCreationService(
        loser_db,
        loser_projects,
        SQLiteWorkflowEventRepository(loser_db),
        FakeGitHubNames(),
        project_id_factory=lambda: ProjectId.from_string(
            "00000000-0000-0000-0000-000000000015"
        ),
        event_id_factory=lambda: WorkflowEventId.from_string(
            "00000000-0000-0000-0000-000000000115"
        ),
    )

    result = loser.create_from_command(command())
    assert result.duplicate and result.project.id == PROJECT_ID
    assert len(winner_projects.list_all()) == 1
    assert (
        winner_db.execute("SELECT count(*) FROM project_creation_context").fetchone()[0]
        == 1
    )
    assert len(winner_events.for_project(PROJECT_ID)) == 1
    assert len(winner_projects.transitions(PROJECT_ID)) == 1
    loser_db.close()
    winner_db.close()
