from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from syntra_build.application.design import ProjectDesignContextService
from syntra_build.domain import (
    DecisionSource,
    DesignMessage,
    DocumentStatus,
    DocumentType,
    MessageDirection,
    MessageId,
    Project,
    ProjectDecision,
    ProjectDecisionId,
    ProjectDocumentId,
    ProjectId,
    ProjectState,
)
from syntra_build.infrastructure.persistence import (
    MIGRATIONS,
    ProjectCreationContext,
    SQLiteDesignMessageRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    apply_migrations,
    document_content_hash,
    open_database,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
PROJECT_A = ProjectId.from_string("00000000-0000-0000-0000-000000000151")
PROJECT_B = ProjectId.from_string("00000000-0000-0000-0000-000000000152")


def uid(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def add_project(
    connection: sqlite3.Connection, project_id: ProjectId, name: str
) -> None:
    SQLiteProjectRepository(connection, lambda: uid(999)).add(
        Project(project_id, name, ProjectState.DESIGNING, NOW, NOW)
    )


def message(
    number: int,
    project_id: ProjectId = PROJECT_A,
    *,
    direction: MessageDirection = MessageDirection.INBOUND,
    occurred_at: datetime = NOW,
    platform: str = "telegram",
    chat_id: str = "chat-a",
    external_id: str | None = None,
) -> DesignMessage:
    return DesignMessage(
        MessageId.from_string(uid(number)),
        project_id,
        direction,
        platform,
        chat_id,
        "TEXT",
        f"text {number}",
        occurred_at,
        f"correlation-{number}",
        external_message_id=external_id,
    )


def decision(
    number: int,
    project_id: ProjectId = PROJECT_A,
    *,
    created_at: datetime = NOW,
    value: str = "SQLite",
    decision_type: str = "DATABASE",
) -> ProjectDecision:
    return ProjectDecision(
        ProjectDecisionId.from_string(uid(number)),
        project_id,
        decision_type,
        f"Decision {number}",
        value,
        "Deterministic rationale",
        DecisionSource.HUMAN,
        created_at,
        "human-1",
    )


def test_messages_are_ordered_isolated_deduplicated_and_durable(tmp_path: Path) -> None:
    path = tmp_path / "design.db"
    with open_database(path) as connection:
        apply_migrations(connection)
        add_project(connection, PROJECT_A, "A")
        add_project(connection, PROJECT_B, "B")
        repository = SQLiteDesignMessageRepository(connection)
        later = message(153, occurred_at=NOW + timedelta(seconds=1))
        first_equal = message(151, external_id="delivery")
        second_equal = message(
            152, direction=MessageDirection.OUTBOUND, external_id=None
        )
        nullable = DesignMessage(
            MessageId.from_string(uid(154)),
            PROJECT_A,
            MessageDirection.INBOUND,
            "telegram",
            "chat-a",
            "TEXT",
            None,
            NOW + timedelta(seconds=2),
            "correlation-null",
        )
        for item in (later, second_equal, first_equal, nullable):
            assert repository.add(item)[1]
        duplicate, inserted = repository.add(message(155, external_id="delivery"))
        assert not inserted and duplicate.id == first_equal.id
        assert repository.get(PROJECT_A, first_equal.id) == first_equal
        assert [item.id for item in repository.for_project(PROJECT_A)] == [
            first_equal.id,
            second_equal.id,
            later.id,
            nullable.id,
        ]
        assert nullable.text is None and nullable.milestone_id is None

        # Provider and chat form part of the durable provider-delivery identity.
        assert repository.add(message(156, platform="other", external_id="delivery"))[1]
        assert repository.add(message(157, chat_id="chat-b", external_id="delivery"))[1]
        repository.add(message(158, PROJECT_B, chat_id="project-b"))
        assert len(repository.for_project(PROJECT_B)) == 1
        with pytest.raises(PersistenceError):
            repository.get(PROJECT_B, first_equal.id)

    with open_database(path) as reopened:
        apply_migrations(reopened)
        assert len(SQLiteDesignMessageRepository(reopened).for_project(PROJECT_A)) == 6


def test_document_revisions_hash_approval_history_and_immutability(
    tmp_path: Path,
) -> None:
    path = tmp_path / "documents.db"
    with open_database(path) as connection:
        apply_migrations(connection)
        add_project(connection, PROJECT_A, "A")
        add_project(connection, PROJECT_B, "B")
        ids = iter(ProjectDocumentId.from_string(uid(n)) for n in range(201, 210))
        documents = SQLiteProjectDocumentRepository(connection, lambda: next(ids))
        exact = "# SPEC\nUnicode: π\n"
        spec1 = documents.create_revision(PROJECT_A, DocumentType.SPEC, exact, NOW, "a")
        agents1 = documents.create_revision(
            PROJECT_A, DocumentType.AGENTS, "agents", NOW, "a"
        )
        other_spec = documents.create_revision(
            PROJECT_B, DocumentType.SPEC, exact, NOW, "a"
        )
        assert (spec1.revision, agents1.revision, other_spec.revision) == (1, 1, 1)
        assert spec1.content_hash == hashlib.sha256(exact.encode("utf-8")).hexdigest()
        assert document_content_hash(exact) == spec1.content_hash
        assert document_content_hash(exact + "changed") != spec1.content_hash
        approved1 = documents.approve(PROJECT_A, spec1.id, NOW, "human")
        assert approved1.status is DocumentStatus.APPROVED
        assert (approved1.approved_at, approved1.approved_by) == (NOW, "human")

        spec2 = documents.create_revision(
            PROJECT_A, DocumentType.SPEC, exact + "v2", NOW, "a"
        )
        assert spec2.revision == 2 and spec2.supersedes_document_id == spec1.id
        approved2 = documents.approve(
            PROJECT_A, spec2.id, NOW + timedelta(seconds=1), "human"
        )
        assert approved2.status is DocumentStatus.APPROVED
        assert documents.get(PROJECT_A, spec1.id).status is DocumentStatus.SUPERSEDED
        rejected = documents.create_revision(
            PROJECT_A, DocumentType.AGENTS, "rejected", NOW, "a"
        )
        assert (
            documents.reject(PROJECT_A, rejected.id).status is DocumentStatus.REJECTED
        )
        assert documents.get(PROJECT_A, rejected.id).content == "rejected"
        assert (
            sum(
                item.status is DocumentStatus.APPROVED
                for item in documents.for_project(PROJECT_A)
                if item.document_type is DocumentType.SPEC
            )
            == 1
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE project_documents SET content='mutated' WHERE id=?",
                (str(spec1.id),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO project_documents
                   (id,project_id,document_type,revision,status,content,content_hash,
                    created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    uid(299),
                    str(PROJECT_A),
                    "SPEC",
                    2,
                    "DRAFT",
                    "x",
                    "0" * 64,
                    NOW.isoformat(),
                    "a",
                ),
            )
        with pytest.raises(PersistenceError):
            documents.create_revision(
                ProjectId.from_string(uid(999)), DocumentType.SPEC, "x", NOW, "a"
            )

    with open_database(path) as reopened:
        apply_migrations(reopened)
        documents = SQLiteProjectDocumentRepository(reopened)
        loaded = documents.get(PROJECT_A, spec1.id)
        assert (loaded.content, loaded.content_hash) == (exact, spec1.content_hash)


def test_decisions_are_validated_ordered_superseded_isolated_and_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "decisions.db"
    with open_database(path) as connection:
        apply_migrations(connection)
        add_project(connection, PROJECT_A, "A")
        add_project(connection, PROJECT_B, "B")
        decisions = SQLiteProjectDecisionRepository(connection)
        old = decision(301)
        later = decision(303, created_at=NOW + timedelta(seconds=1))
        replacement = decision(302, value="PostgreSQL")
        decisions.add(later)
        decisions.add(old)
        decisions.supersede(PROJECT_A, old.id, replacement)
        decisions.add(decision(304, PROJECT_B))
        assert [item.id for item in decisions.for_project(PROJECT_A)] == [
            old.id,
            replacement.id,
            later.id,
        ]
        assert [
            item.id for item in decisions.for_project(PROJECT_A, active_only=True)
        ] == [
            replacement.id,
            later.id,
        ]
        assert (
            decisions.for_project(PROJECT_A)[0].superseded_by_decision_id
            == replacement.id
        )
        assert len(decisions.for_project(PROJECT_B)) == 1
        assert decisions.repository_visibility(PROJECT_A) == "public"
        visibility = decision(
            305, value="private", decision_type="REPOSITORY_VISIBILITY"
        )
        decisions.add(visibility)
        assert decisions.repository_visibility(PROJECT_A) == "private"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO project_decisions
                   (id,project_id,decision_type,title,decision,rationale,source,
                    created_at,created_by) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    uid(399),
                    str(PROJECT_A),
                    "X",
                    "X",
                    "X",
                    "X",
                    "UNTRUSTED",
                    NOW.isoformat(),
                    "x",
                ),
            )

    with open_database(path) as reopened:
        apply_migrations(reopened)
        assert (
            len(SQLiteProjectDecisionRepository(reopened).for_project(PROJECT_A)) == 4
        )


def test_structured_context_reconstruction_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "context.db"

    def reconstruct(connection: sqlite3.Connection) -> tuple[object, ...]:
        service = ProjectDesignContextService(
            SQLiteProjectRepository(connection, lambda: uid(900)),
            SQLiteDesignMessageRepository(connection),
            SQLiteProjectDecisionRepository(connection),
            SQLiteProjectDocumentRepository(connection),
        )
        context = service.reconstruct(PROJECT_A)
        return (
            context.project.id,
            context.creation_context.initial_request
            if context.creation_context is not None
            else None,
            tuple(item.text for item in context.messages),
            tuple(item.decision for item in context.decisions),
            tuple((item.document_type, item.revision) for item in context.documents),
        )

    with open_database(path) as connection:
        apply_migrations(connection)
        add_project(connection, PROJECT_A, "A")
        add_project(connection, PROJECT_B, "B")
        SQLiteProjectRepository(connection, lambda: uid(900)).add_creation_context(
            ProjectCreationContext(
                PROJECT_A,
                "owner-a",
                "Build project A",
                "telegram",
                "chat-a",
                None,
                "update-a",
                "message-a",
                NOW,
            )
        )
        messages = SQLiteDesignMessageRepository(connection)
        messages.add(message(401, occurred_at=NOW + timedelta(seconds=1)))
        messages.add(message(402))
        messages.add(message(403, PROJECT_B, chat_id="b"))
        decisions = SQLiteProjectDecisionRepository(connection)
        decisions.add(decision(404))
        decisions.add(decision(405, PROJECT_B))
        ids = iter(ProjectDocumentId.from_string(uid(n)) for n in range(406, 411))
        documents = SQLiteProjectDocumentRepository(connection, lambda: next(ids))
        documents.create_revision(PROJECT_A, DocumentType.SPEC, "spec 1", NOW, "a")
        documents.create_revision(PROJECT_A, DocumentType.SPEC, "spec 2", NOW, "a")
        documents.create_revision(PROJECT_A, DocumentType.AGENTS, "agents", NOW, "a")
        documents.create_revision(PROJECT_B, DocumentType.SPEC, "foreign", NOW, "a")
        before = reconstruct(connection)
        assert before[0] == PROJECT_A
        assert before[1] == "Build project A"
        assert before[2] == ("text 402", "text 401")
        assert before[3] == ("SQLite",)
        assert before[4] == ((DocumentType.AGENTS, 1), (DocumentType.SPEC, 2))

    with open_database(path) as reopened:
        apply_migrations(reopened)
        assert reconstruct(reopened) == before


def test_m14_1_upgrade_preserves_projects_and_provider_cursor(tmp_path: Path) -> None:
    with open_database(tmp_path / "upgrade.db") as connection:
        apply_migrations(connection, MIGRATIONS[:-1])
        connection.execute(
            """INSERT INTO projects
               (id,name,state,created_at,updated_at,last_state_change_at,canonical_name)
               VALUES (?,?,?,?,?,?,?)""",
            (
                str(PROJECT_A),
                "existing",
                "DESIGNING",
                *(NOW.isoformat(),) * 3,
                "existing",
            ),
        )
        connection.execute(
            "INSERT INTO provider_cursors VALUES ('telegram',42,?)", (NOW.isoformat(),)
        )
        apply_migrations(connection)
        assert (
            connection.execute(
                "SELECT name FROM projects WHERE id=?", (str(PROJECT_A),)
            ).fetchone()[0]
            == "existing"
        )
        assert (
            connection.execute(
                """SELECT last_processed_update_id FROM provider_cursors
                   WHERE provider='telegram'"""
            ).fetchone()[0]
            == 42
        )
