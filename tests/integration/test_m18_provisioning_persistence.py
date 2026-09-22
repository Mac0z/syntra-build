import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.domain import Project, ProjectId, ProjectState, RepositoryVisibility
from syntra_build.infrastructure.persistence import (
    SQLiteProjectRepository,
    SQLiteProvisioningRepository,
    apply_migrations,
    open_database,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PID = ProjectId(UUID(int=1))


@pytest.fixture
def repository(
    tmp_path: Path,
) -> Iterator[tuple[sqlite3.Connection, SQLiteProvisioningRepository]]:
    connection = open_database(tmp_path / "state.db")
    apply_migrations(connection)
    SQLiteProjectRepository(connection, lambda: "transition").add(
        Project(
            PID,
            "My Cool App",
            ProjectState.PROVISIONING,
            NOW,
            NOW,
            canonical_name="my-cool-app",
        )
    )
    connection.commit()
    yield connection, SQLiteProvisioningRepository(connection)
    connection.close()


def test_intended_identity_is_durable_and_idempotent(
    repository: tuple[sqlite3.Connection, SQLiteProvisioningRepository],
) -> None:
    connection, records = repository
    first = records.ensure_intent(
        PID, "Mac0z", "my-cool-app", RepositoryVisibility.PUBLIC, NOW
    )
    connection.commit()
    second = records.ensure_intent(
        PID, "Mac0z", "my-cool-app", RepositoryVisibility.PUBLIC, NOW
    )

    assert first == second
    assert first.full_name == "Mac0z/my-cool-app"
    assert first.external_repository_id is None
    project = SQLiteProjectRepository(connection, lambda: "transition").get(PID)
    assert project.name == "My Cool App"
    assert project.canonical_name == "my-cool-app"


def test_persisted_intent_cannot_be_rederived_differently(
    repository: tuple[sqlite3.Connection, SQLiteProvisioningRepository],
) -> None:
    connection, records = repository
    records.ensure_intent(PID, "Mac0z", "my-cool-app", RepositoryVisibility.PUBLIC, NOW)
    connection.commit()

    with pytest.raises(PersistenceError, match="does not match"):
        records.ensure_intent(
            PID, "Other", "my-cool-app", RepositoryVisibility.PUBLIC, NOW
        )


def test_external_repository_id_is_immutable(
    repository: tuple[sqlite3.Connection, SQLiteProvisioningRepository],
) -> None:
    connection, records = repository
    intended = records.ensure_intent(
        PID, "Mac0z", "my-cool-app", RepositoryVisibility.PUBLIC, NOW
    )
    records.identify(intended.id, 123, "main", NOW)
    connection.commit()

    with pytest.raises(PersistenceError, match="identity"):
        records.identify(intended.id, 456, "main", NOW)
