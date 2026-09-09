"""SQLite repositories for durable, project-isolated design artifacts."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from syntra_build.domain import (
    DecisionSource,
    DesignMessage,
    DocumentStatus,
    DocumentType,
    GateId,
    MessageDirection,
    MessageId,
    MilestoneId,
    ProjectDecision,
    ProjectDecisionId,
    ProjectDocument,
    ProjectDocumentId,
    ProjectId,
)
from syntra_build.domain._validation import require_utc
from syntra_build.infrastructure.persistence.connection import transaction_scope
from syntra_build.infrastructure.persistence.errors import PersistenceError


def document_content_hash(content: str) -> str:
    """Hash the exact persisted Unicode text after deterministic UTF-8 encoding."""
    if not isinstance(content, str):
        raise TypeError("document content must be a string")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    require_utc(value, "timestamp")
    return value.isoformat(timespec="microseconds")


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def _required_datetime(value: str) -> datetime:
    parsed = _datetime(value)
    assert parsed is not None
    return parsed


def _message(row: sqlite3.Row) -> DesignMessage:
    direction = MessageDirection(row["direction"])
    occurred = (
        row["received_at"] if direction is MessageDirection.INBOUND else row["sent_at"]
    )
    return DesignMessage(
        MessageId.from_string(row["id"]),
        ProjectId.from_string(row["project_id"]),
        direction,
        row["platform"],
        row["chat_id"],
        row["message_type"],
        row["text"],
        _required_datetime(occurred),
        row["correlation_id"],
        MilestoneId.from_string(row["milestone_id"]) if row["milestone_id"] else None,
        GateId.from_string(row["gate_id"]) if row["gate_id"] else None,
        row["external_message_id"],
        row["thread_id"],
        row["sender_id"],
        row["attachments_json"],
        row["reply_to_message_id"],
        row["raw_metadata_json"],
    )


class SQLiteDesignMessageRepository:
    """Persist messages and deduplicate provider redelivery durably."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, message: DesignMessage) -> tuple[DesignMessage, bool]:
        received_at = (
            _timestamp(message.occurred_at)
            if message.direction is MessageDirection.INBOUND
            else None
        )
        sent_at = (
            _timestamp(message.occurred_at)
            if message.direction is MessageDirection.OUTBOUND
            else None
        )
        try:
            self._connection.execute(
                """INSERT INTO messages
                   (id,project_id,milestone_id,gate_id,direction,platform,
                    external_message_id,chat_id,thread_id,sender_id,message_type,text,
                    attachments_json,reply_to_message_id,received_at,sent_at,
                    correlation_id,raw_metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(message.id),
                    str(message.project_id),
                    str(message.milestone_id) if message.milestone_id else None,
                    str(message.gate_id) if message.gate_id else None,
                    message.direction.value,
                    message.platform,
                    message.external_message_id,
                    message.chat_id,
                    message.thread_id,
                    message.sender_id,
                    message.message_type,
                    message.text,
                    message.attachments_json,
                    message.reply_to_message_id,
                    received_at,
                    sent_at,
                    message.correlation_id,
                    message.raw_metadata_json,
                ),
            )
            return message, True
        except sqlite3.IntegrityError as error:
            if message.external_message_id is not None:
                existing = self._connection.execute(
                    """SELECT * FROM messages WHERE platform=? AND chat_id=?
                       AND external_message_id=?""",
                    (message.platform, message.chat_id, message.external_message_id),
                ).fetchone()
                if existing is not None:
                    if existing["project_id"] != str(message.project_id):
                        raise PersistenceError(
                            "provider delivery identity belongs to another project"
                        ) from error
                    return _message(existing), False
            raise PersistenceError("design message could not be stored") from error
        except sqlite3.Error as error:
            raise PersistenceError("design message could not be stored") from error

    def get(self, project_id: ProjectId, message_id: MessageId) -> DesignMessage:
        row = self._connection.execute(
            "SELECT * FROM messages WHERE project_id=? AND id=?",
            (str(project_id), str(message_id)),
        ).fetchone()
        if row is None:
            raise PersistenceError("design message does not exist for project")
        return _message(row)

    def for_project(self, project_id: ProjectId) -> tuple[DesignMessage, ...]:
        rows = self._connection.execute(
            """SELECT * FROM messages WHERE project_id=?
               ORDER BY coalesce(received_at,sent_at),id""",
            (str(project_id),),
        ).fetchall()
        return tuple(_message(row) for row in rows)


def _document(row: sqlite3.Row) -> ProjectDocument:
    return ProjectDocument(
        ProjectDocumentId.from_string(row["id"]),
        ProjectId.from_string(row["project_id"]),
        DocumentType(row["document_type"]),
        int(row["revision"]),
        DocumentStatus(row["status"]),
        row["content"],
        row["content_hash"],
        _required_datetime(row["created_at"]),
        row["created_by"],
        _datetime(row["approved_at"]),
        row["approved_by"],
        ProjectDocumentId.from_string(row["supersedes_document_id"])
        if row["supersedes_document_id"]
        else None,
    )


class SQLiteProjectDocumentRepository:
    """Create immutable revisions and atomically maintain approval uniqueness."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        id_factory: Callable[[], ProjectDocumentId] = ProjectDocumentId.generate,
    ) -> None:
        self._connection = connection
        self._id_factory = id_factory

    def create_revision(
        self,
        project_id: ProjectId,
        document_type: DocumentType,
        content: str,
        created_at: datetime,
        created_by: str,
    ) -> ProjectDocument:
        try:
            with transaction_scope(self._connection):
                prior = self._connection.execute(
                    """SELECT id,revision FROM project_documents
                       WHERE project_id=? AND document_type=?
                       ORDER BY revision DESC LIMIT 1""",
                    (str(project_id), document_type.value),
                ).fetchone()
                revision = 1 if prior is None else int(prior["revision"]) + 1
                document = ProjectDocument(
                    self._id_factory(),
                    project_id,
                    document_type,
                    revision,
                    DocumentStatus.DRAFT,
                    content,
                    document_content_hash(content),
                    created_at,
                    created_by,
                    supersedes_document_id=(
                        ProjectDocumentId.from_string(prior["id"]) if prior else None
                    ),
                )
                self._connection.execute(
                    """INSERT INTO project_documents
                       (id,project_id,document_type,revision,status,content,content_hash,
                        created_at,created_by,supersedes_document_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(document.id),
                        str(project_id),
                        document_type.value,
                        revision,
                        document.status.value,
                        content,
                        document.content_hash,
                        _timestamp(created_at),
                        created_by,
                        str(document.supersedes_document_id)
                        if document.supersedes_document_id
                        else None,
                    ),
                )
            return document
        except sqlite3.Error as error:
            raise PersistenceError("document revision could not be created") from error

    def get(
        self, project_id: ProjectId, document_id: ProjectDocumentId
    ) -> ProjectDocument:
        row = self._connection.execute(
            "SELECT * FROM project_documents WHERE project_id=? AND id=?",
            (str(project_id), str(document_id)),
        ).fetchone()
        if row is None:
            raise PersistenceError("project document does not exist")
        return _document(row)

    def for_project(self, project_id: ProjectId) -> tuple[ProjectDocument, ...]:
        rows = self._connection.execute(
            """SELECT * FROM project_documents WHERE project_id=?
               ORDER BY document_type,revision""",
            (str(project_id),),
        ).fetchall()
        return tuple(_document(row) for row in rows)

    def current_for_project(self, project_id: ProjectId) -> tuple[ProjectDocument, ...]:
        rows = self._connection.execute(
            """SELECT d.* FROM project_documents d WHERE d.project_id=?
               AND d.revision=(SELECT max(n.revision) FROM project_documents n
                 WHERE n.project_id=d.project_id AND n.document_type=d.document_type)
               ORDER BY d.document_type""",
            (str(project_id),),
        ).fetchall()
        return tuple(_document(row) for row in rows)

    def approve(
        self,
        project_id: ProjectId,
        document_id: ProjectDocumentId,
        approved_at: datetime,
        approved_by: str,
    ) -> ProjectDocument:
        try:
            with transaction_scope(self._connection):
                row = self._connection.execute(
                    "SELECT * FROM project_documents WHERE project_id=? AND id=?",
                    (str(project_id), str(document_id)),
                ).fetchone()
                if row is None:
                    raise PersistenceError("project document does not exist")
                if row["status"] != DocumentStatus.DRAFT.value:
                    raise PersistenceError("only a draft document can be approved")
                self._connection.execute(
                    """UPDATE project_documents SET status='SUPERSEDED'
                       WHERE project_id=? AND document_type=? AND status='APPROVED'""",
                    (str(project_id), row["document_type"]),
                )
                self._connection.execute(
                    """UPDATE project_documents
                       SET status='APPROVED',approved_at=?,approved_by=?
                       WHERE project_id=? AND id=? AND status='DRAFT'""",
                    (
                        _timestamp(approved_at),
                        approved_by,
                        str(project_id),
                        str(document_id),
                    ),
                )
            return self.get(project_id, document_id)
        except sqlite3.Error as error:
            raise PersistenceError("document could not be approved") from error

    def reject(
        self, project_id: ProjectId, document_id: ProjectDocumentId
    ) -> ProjectDocument:
        try:
            changed = self._connection.execute(
                """UPDATE project_documents SET status='REJECTED'
                   WHERE project_id=? AND id=? AND status='DRAFT'""",
                (str(project_id), str(document_id)),
            ).rowcount
        except sqlite3.Error as error:
            raise PersistenceError("document could not be rejected") from error
        if changed != 1:
            raise PersistenceError("only a draft document can be rejected")
        return self.get(project_id, document_id)


def _decision(row: sqlite3.Row) -> ProjectDecision:
    return ProjectDecision(
        ProjectDecisionId.from_string(row["id"]),
        ProjectId.from_string(row["project_id"]),
        row["decision_type"],
        row["title"],
        row["decision"],
        row["rationale"],
        DecisionSource(row["source"]),
        _required_datetime(row["created_at"]),
        row["created_by"],
        MilestoneId.from_string(row["milestone_id"]) if row["milestone_id"] else None,
        ProjectDecisionId.from_string(row["superseded_by_decision_id"])
        if row["superseded_by_decision_id"]
        else None,
    )


class SQLiteProjectDecisionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, decision: ProjectDecision) -> None:
        try:
            self._connection.execute(
                """INSERT INTO project_decisions
                   (id,project_id,milestone_id,decision_type,title,decision,rationale,
                    source,created_at,created_by,superseded_by_decision_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(decision.id),
                    str(decision.project_id),
                    str(decision.milestone_id) if decision.milestone_id else None,
                    decision.decision_type,
                    decision.title,
                    decision.decision,
                    decision.rationale,
                    decision.source.value,
                    _timestamp(decision.created_at),
                    decision.created_by,
                    str(decision.superseded_by_decision_id)
                    if decision.superseded_by_decision_id
                    else None,
                ),
            )
        except sqlite3.Error as error:
            raise PersistenceError("project decision could not be stored") from error

    def for_project(
        self, project_id: ProjectId, *, active_only: bool = False
    ) -> tuple[ProjectDecision, ...]:
        predicate = " AND superseded_by_decision_id IS NULL" if active_only else ""
        rows = self._connection.execute(
            f"""SELECT * FROM project_decisions WHERE project_id=?{predicate}
                 ORDER BY created_at,id""",
            (str(project_id),),
        ).fetchall()
        return tuple(_decision(row) for row in rows)

    def supersede(
        self,
        project_id: ProjectId,
        previous_id: ProjectDecisionId,
        replacement: ProjectDecision,
    ) -> None:
        if replacement.project_id != project_id:
            raise PersistenceError("replacement decision belongs to another project")
        try:
            with transaction_scope(self._connection):
                self.add(replacement)
                changed = self._connection.execute(
                    """UPDATE project_decisions SET superseded_by_decision_id=?
                       WHERE project_id=? AND id=?
                       AND superseded_by_decision_id IS NULL""",
                    (str(replacement.id), str(project_id), str(previous_id)),
                ).rowcount
                if changed != 1:
                    raise PersistenceError(
                        "active decision to supersede does not exist"
                    )
        except sqlite3.Error as error:
            raise PersistenceError("decision supersession failed") from error

    def repository_visibility(self, project_id: ProjectId) -> str:
        """Return the explicit active choice, otherwise M15's safe public default."""
        row = self._connection.execute(
            """SELECT decision FROM project_decisions WHERE project_id=?
               AND decision_type='REPOSITORY_VISIBILITY'
               AND superseded_by_decision_id IS NULL
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (str(project_id),),
        ).fetchone()
        if row is None:
            return "public"
        value = str(row[0]).casefold()
        if value not in {"public", "private"}:
            raise PersistenceError("repository visibility decision is invalid")
        return value
