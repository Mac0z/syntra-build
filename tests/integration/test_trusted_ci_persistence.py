"""Trusted bootstrap intent remains exact and additive across restarts/upgrades."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from syntra_build.domain import (
    DocumentType,
    Project,
    ProjectId,
    ProjectState,
    RepositoryVisibility,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.provisioning import RepositoryBaseline, RepositoryCIIntent
from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    SQLiteProvisioningRepository,
    apply_migrations,
    current_schema_version,
    open_database,
    transaction,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError

NOW = datetime(2026, 10, 11, tzinfo=UTC)
PID = ProjectId(UUID(int=1))
WORKFLOW = "name: CI\npermissions:\n  contents: read\n"


def _seed(
    connection: sqlite3.Connection, project_id: ProjectId = PID
) -> RepositoryCIIntent:
    SQLiteProjectRepository(connection, lambda: "transition").add(
        Project(
            project_id,
            "Project",
            ProjectState.PROVISIONING,
            NOW,
            NOW,
            canonical_name=f"project-{project_id}",
        )
    )
    documents = SQLiteProjectDocumentRepository(connection)
    spec = documents.create_revision(
        project_id, DocumentType.SPEC, "Python 3.14; pytest", NOW, "architect"
    )
    agents = documents.create_revision(
        project_id, DocumentType.AGENTS, "Run pytest", NOW, "architect"
    )
    spec = documents.approve(project_id, spec.id, NOW, "human")
    agents = documents.approve(project_id, agents.id, NOW, "human")
    repository = SQLiteProvisioningRepository(connection).ensure_intent(
        project_id, "owner", f"project-{project_id}", RepositoryVisibility.PUBLIC, NOW
    )
    return RepositoryCIIntent(
        project_id,
        repository.id,
        spec.id,
        spec.revision,
        spec.content_hash,
        agents.id,
        agents.revision,
        agents.content_hash,
        "python-pytest-v1",
        "3.14",
        ".github/workflows/ci.yml",
        WORKFLOW,
        sha256(WORKFLOW.encode()).hexdigest(),
        "main",
        NOW,
    )


def test_clean_schema_persists_exact_intent_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    connection = open_database(path)
    assert apply_migrations(connection) == len(MIGRATIONS)
    intent = _seed(connection)
    with transaction(connection):
        SQLiteProvisioningRepository(connection).save_ci_intent(intent)
    connection.close()

    with open_database(path) as reopened:
        records = SQLiteProvisioningRepository(reopened)
        assert records.ci_intent(PID) == intent
        records.save_ci_intent(replace(intent, created_at=NOW + timedelta(hours=1)))
        assert records.ci_intent(PID) == intent
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_id", "other-profile"),
        ("python_version", "3.13"),
        ("workflow_content", WORKFLOW + "# changed\n"),
    ],
)
def test_conflicting_replay_cannot_replace_intent(
    tmp_path: Path, field: str, value: str
) -> None:
    with open_database(tmp_path / "state.db") as connection:
        apply_migrations(connection)
        intent = _seed(connection)
        records = SQLiteProvisioningRepository(connection)
        records.save_ci_intent(intent)
        if field == "profile_id":
            changed = replace(intent, profile_id=value)
        elif field == "python_version":
            changed = replace(intent, python_version=value)
        else:
            changed = replace(
                intent,
                workflow_content=value,
                workflow_content_hash=sha256(value.encode()).hexdigest(),
            )
        with pytest.raises(PersistenceError, match="does not match"):
            records.save_ci_intent(changed)
        assert records.ci_intent(PID) == intent


def test_intent_rejects_foreign_repository_and_documents(tmp_path: Path) -> None:
    with open_database(tmp_path / "state.db") as connection:
        apply_migrations(connection)
        intent = _seed(connection)
        foreign = _seed(connection, ProjectId(UUID(int=2)))
        records = SQLiteProvisioningRepository(connection)
        with pytest.raises(PersistenceError, match="could not be persisted"):
            records.save_ci_intent(
                replace(intent, github_repository_id=foreign.github_repository_id)
            )
        with pytest.raises(PersistenceError, match="approved documents"):
            records.save_ci_intent(
                replace(intent, spec_document_id=foreign.spec_document_id)
            )
        assert records.ci_intent(PID) is None


def test_intent_requires_approved_exact_document_revision_and_hash(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "state.db") as connection:
        apply_migrations(connection)
        intent = _seed(connection)
        records = SQLiteProvisioningRepository(connection)
        for invalid in (
            replace(intent, spec_content_hash="0" * 64),
            replace(intent, agents_revision=2),
            replace(intent, agents_document_id=intent.spec_document_id),
        ):
            with pytest.raises(PersistenceError, match="approved documents"):
                records.save_ci_intent(invalid)
        draft = SQLiteProjectDocumentRepository(connection).create_revision(
            PID, DocumentType.SPEC, "new draft", NOW, "architect"
        )
        with pytest.raises(PersistenceError, match="approved documents"):
            records.save_ci_intent(
                replace(
                    intent,
                    spec_document_id=draft.id,
                    spec_revision=draft.revision,
                    spec_content_hash=draft.content_hash,
                )
            )
        assert records.ci_intent(PID) is None


def test_intent_is_immutable_even_through_direct_sql(tmp_path: Path) -> None:
    with open_database(tmp_path / "state.db") as connection:
        apply_migrations(connection)
        intent = _seed(connection)
        records = SQLiteProvisioningRepository(connection)
        records.save_ci_intent(intent)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """UPDATE repository_ci_intents SET python_version='3.13'
                WHERE project_id=?""",
                (str(PID),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="preservation-oriented"):
            connection.execute(
                "DELETE FROM repository_ci_intents WHERE project_id=?", (str(PID),)
            )
        assert records.ci_intent(PID) == intent


def test_domain_rejects_workflow_hash_mismatch_and_unsafe_target(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "state.db") as connection:
        apply_migrations(connection)
        intent = _seed(connection)
        with pytest.raises(DomainValidationError):
            replace(intent, workflow_content_hash="0" * 64)
        with pytest.raises(DomainValidationError):
            replace(intent, workflow_path="../ci.yml")
        with pytest.raises(DomainValidationError):
            replace(intent, default_branch="other")


def test_schema_28_upgrade_preserves_old_baseline_without_inventing_ci(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "upgrade.db") as connection:
        apply_migrations(connection, MIGRATIONS[:28])
        intent = _seed(connection)
        records = SQLiteProvisioningRepository(connection)
        baseline = RepositoryBaseline(
            "old-baseline",
            PID,
            intent.github_repository_id,
            "a" * 40,
            intent.spec_document_id,
            intent.spec_revision,
            intent.spec_content_hash,
            intent.agents_document_id,
            intent.agents_revision,
            intent.agents_content_hash,
            NOW,
        )
        records.identify(intent.github_repository_id, 42, "main", NOW)
        records.save_baseline(baseline)
        records.verify(intent.github_repository_id, baseline.id, NOW)
        old_tables = (
            "github_repositories",
            "repository_baselines",
            "project_documents",
        )
        before = {
            table: tuple(
                tuple(row) for row in connection.execute(f"SELECT * FROM {table}")
            )
            for table in old_tables
        }

        assert apply_migrations(connection) == len(MIGRATIONS)
        assert current_schema_version(connection) == len(MIGRATIONS)
        assert records.ci_intent(PID) is None
        for table in old_tables:
            assert (
                tuple(
                    tuple(row) for row in connection.execute(f"SELECT * FROM {table}")
                )
                == before[table]
            )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_baseline_replay_rejects_conflicting_identity_without_rewriting(
    tmp_path: Path,
) -> None:
    with open_database(tmp_path / "baseline-replay.db") as connection:
        apply_migrations(connection)
        intent = _seed(connection)
        records = SQLiteProvisioningRepository(connection)
        baseline = RepositoryBaseline(
            "baseline",
            PID,
            intent.github_repository_id,
            "a" * 40,
            intent.spec_document_id,
            intent.spec_revision,
            intent.spec_content_hash,
            intent.agents_document_id,
            intent.agents_revision,
            intent.agents_content_hash,
            NOW,
        )
        records.save_baseline(baseline)
        records.save_baseline(
            replace(baseline, created_at=NOW + timedelta(hours=1), verified_at=NOW)
        )
        assert records.baseline(PID) == baseline
        for conflicting in (
            replace(baseline, id="different-baseline"),
            replace(baseline, commit_sha="b" * 40),
            replace(baseline, spec_content_hash="0" * 64),
            replace(baseline, agents_revision=2),
        ):
            with pytest.raises(PersistenceError, match="does not match"):
                records.save_baseline(conflicting)
            assert records.baseline(PID) == baseline
