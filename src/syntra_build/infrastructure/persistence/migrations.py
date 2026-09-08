# ruff: noqa: E501
"""Small, ordered migration runner for Syntra Build's SQLite schema."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import MigrationError

_LOGGER = logging.getLogger(__name__)
_MIGRATION_TABLE = "schema_migrations"


@dataclass(frozen=True, slots=True)
class Migration:
    """One source-controlled schema change consisting only of trusted SQL."""

    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="001_initial",
        statements=(
            """CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            ) STRICT""",
        ),
    ),
    Migration(
        version=2,
        name="002_project_state_machine",
        statements=(
            """CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'NEW','DESIGNING','DESIGN_APPROVAL','PROVISIONING','READY',
                    'BUILDING','WAITING_HUMAN','PAUSED','BLOCKED','COMPLETING',
                    'COMPLETE','FAILED','CANCELLED'
                )),
                resume_state TEXT CHECK (resume_state IS NULL OR resume_state IN (
                    'NEW','DESIGNING','DESIGN_APPROVAL','PROVISIONING','READY',
                    'BUILDING','WAITING_HUMAN','BLOCKED','COMPLETING'
                )),
                activity TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_state_change_at TEXT NOT NULL
            ) STRICT""",
            """CREATE TABLE state_transitions (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL CHECK (entity_type = 'PROJECT'),
                entity_id TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id),
                previous_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                trigger_event_id TEXT,
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                correlation_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT,
                CHECK (entity_id = project_id)
            ) STRICT""",
            """CREATE INDEX state_transitions_project_created
                ON state_transitions(project_id, created_at)""",
            """CREATE TRIGGER state_transitions_no_update
                BEFORE UPDATE ON state_transitions BEGIN
                SELECT RAISE(ABORT, 'state transitions are append-only'); END""",
            """CREATE TRIGGER state_transitions_no_delete
                BEFORE DELETE ON state_transitions BEGIN
                SELECT RAISE(ABORT, 'state transitions are append-only'); END""",
        ),
    ),
    Migration(
        version=3,
        name="003_milestone_state_machine",
        statements=(
            "DROP TRIGGER state_transitions_no_update",
            "DROP TRIGGER state_transitions_no_delete",
            "DROP INDEX state_transitions_project_created",
            "ALTER TABLE state_transitions RENAME TO state_transitions_m7",
            """CREATE TABLE milestones (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
                code TEXT NOT NULL,
                title TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'PENDING','READY','PREPARING_TASK','PREPARING_WORKSPACE','CODING',
                    'VALIDATING_CHANGES','COMMITTING','PUSHING','PR_CREATING',
                    'CI_RUNNING','CI_REWORK','ARCHITECT_REVIEW','REVIEW_REWORK',
                    'HUMAN_DECISION','HUMAN_TEST','MERGE_READY','MERGING',
                    'MERGE_VERIFY','COMPLETE','BLOCKED','FAILED','CANCELLED')),
                resume_state TEXT CHECK (resume_state IS NULL OR resume_state IN (
                    'PENDING','READY','PREPARING_TASK','PREPARING_WORKSPACE','CODING',
                    'VALIDATING_CHANGES','COMMITTING','PUSHING','PR_CREATING',
                    'CI_RUNNING','CI_REWORK','ARCHITECT_REVIEW','REVIEW_REWORK',
                    'HUMAN_DECISION','HUMAN_TEST','MERGE_READY','MERGING','MERGE_VERIFY')),
                activity TEXT,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, code), UNIQUE(project_id, sequence_number)
            ) STRICT""",
            """CREATE UNIQUE INDEX one_active_milestone_per_project
                ON milestones(project_id) WHERE state NOT IN
                ('PENDING','COMPLETE','FAILED','CANCELLED')""",
            """CREATE TABLE milestone_dependencies (
                milestone_id TEXT NOT NULL REFERENCES milestones(id),
                depends_on_milestone_id TEXT NOT NULL REFERENCES milestones(id),
                PRIMARY KEY (milestone_id, depends_on_milestone_id),
                CHECK (milestone_id <> depends_on_milestone_id)
            ) STRICT""",
            """CREATE TABLE state_transitions (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL CHECK (
                    entity_type IN ('PROJECT','MILESTONE')),
                entity_id TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id),
                milestone_id TEXT REFERENCES milestones(id),
                previous_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                trigger_event_id TEXT,
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                correlation_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT,
                CHECK ((entity_type='PROJECT' AND entity_id=project_id
                        AND milestone_id IS NULL)
                    OR (entity_type='MILESTONE' AND entity_id=milestone_id
                        AND milestone_id IS NOT NULL))
            ) STRICT""",
            """INSERT INTO state_transitions
                (id,entity_type,entity_id,project_id,milestone_id,previous_state,new_state,
                 reason,trigger_event_id,actor_type,actor_id,correlation_id,created_at,metadata_json)
                SELECT id,entity_type,entity_id,project_id,NULL,
                 previous_state,new_state,
                 reason,trigger_event_id,actor_type,actor_id,correlation_id,created_at,metadata_json
                FROM state_transitions_m7""",
            "DROP TABLE state_transitions_m7",
            """CREATE INDEX state_transitions_project_created
                ON state_transitions(project_id, created_at)""",
            """CREATE INDEX state_transitions_milestone_created
                ON state_transitions(milestone_id, created_at)""",
            """CREATE TRIGGER state_transitions_no_update
                BEFORE UPDATE ON state_transitions BEGIN
                SELECT RAISE(ABORT, 'state transitions are append-only'); END""",
            """CREATE TRIGGER state_transitions_no_delete
                BEFORE DELETE ON state_transitions BEGIN
                SELECT RAISE(ABORT, 'state transitions are append-only'); END""",
        ),
    ),
    Migration(
        version=4,
        name="004_job_state_machine",
        statements=(
            "DROP TRIGGER state_transitions_no_update",
            "DROP TRIGGER state_transitions_no_delete",
            "DROP INDEX state_transitions_project_created",
            "DROP INDEX state_transitions_milestone_created",
            "ALTER TABLE state_transitions RENAME TO state_transitions_m8",
            "CREATE UNIQUE INDEX milestones_project_identity ON milestones(project_id,id)",
            """CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                milestone_id TEXT REFERENCES milestones(id),
                job_type TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('QUEUED','DISPATCHED','RUNNING',
                    'WAITING_EXTERNAL','SUCCEEDED','RETRY_WAIT','FAILED','CANCELLED','ABANDONED')),
                priority INTEGER NOT NULL CHECK(priority >= 0), correlation_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL CHECK(attempt_number >= 0),
                max_attempts INTEGER NOT NULL CHECK(max_attempts >= 1 AND attempt_number <= max_attempts),
                scheduled_at TEXT, started_at TEXT, completed_at TEXT, next_retry_at TEXT,
                timeout_seconds INTEGER CHECK(timeout_seconds IS NULL OR timeout_seconds > 0),
                worker_class TEXT NOT NULL CHECK(worker_class IN
                    ('ARCHITECT','CODEX','GIT','GITHUB','CI','MESSAGING','RECOVERY','INTERNAL')),
                payload_json TEXT NOT NULL, result_json TEXT NOT NULL, last_error_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id,milestone_id) REFERENCES milestones(project_id,id)
            ) STRICT""",
            """CREATE TABLE job_attempts (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
                state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCEEDED','RETRYABLE_FAILURE',
                    'FAILED','CANCELLED','ABANDONED')),
                started_at TEXT NOT NULL, completed_at TEXT, external_request_id TEXT,
                process_id TEXT, exit_code INTEGER, result_json TEXT NOT NULL,
                error_id TEXT, logs_reference TEXT, UNIQUE(job_id,attempt_number)
            ) STRICT""",
            """CREATE TABLE state_transitions (
                id TEXT PRIMARY KEY, entity_type TEXT NOT NULL CHECK(entity_type IN ('PROJECT','MILESTONE','JOB')),
                entity_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
                milestone_id TEXT REFERENCES milestones(id), job_id TEXT REFERENCES jobs(id),
                previous_state TEXT NOT NULL, new_state TEXT NOT NULL, reason TEXT NOT NULL,
                trigger_event_id TEXT, actor_type TEXT NOT NULL, actor_id TEXT,
                correlation_id TEXT NOT NULL, created_at TEXT NOT NULL, metadata_json TEXT,
                CHECK ((entity_type='PROJECT' AND entity_id=project_id AND milestone_id IS NULL AND job_id IS NULL)
                    OR (entity_type='MILESTONE' AND entity_id=milestone_id AND milestone_id IS NOT NULL AND job_id IS NULL)
                    OR (entity_type='JOB' AND entity_id=job_id AND job_id IS NOT NULL))
            ) STRICT""",
            """INSERT INTO state_transitions
                (id,entity_type,entity_id,project_id,milestone_id,job_id,previous_state,new_state,
                 reason,trigger_event_id,actor_type,actor_id,correlation_id,created_at,metadata_json)
                SELECT id,entity_type,entity_id,project_id,milestone_id,NULL,previous_state,new_state,
                 reason,trigger_event_id,actor_type,actor_id,correlation_id,created_at,metadata_json
                FROM state_transitions_m8""",
            "DROP TABLE state_transitions_m8",
            "CREATE INDEX state_transitions_project_created ON state_transitions(project_id,created_at)",
            "CREATE INDEX state_transitions_milestone_created ON state_transitions(milestone_id,created_at)",
            "CREATE INDEX state_transitions_job_created ON state_transitions(job_id,created_at)",
            "CREATE INDEX jobs_project_state ON jobs(project_id,state)",
            """CREATE TRIGGER state_transitions_no_update BEFORE UPDATE ON state_transitions BEGIN
                SELECT RAISE(ABORT,'state transitions are append-only'); END""",
            """CREATE TRIGGER state_transitions_no_delete BEFORE DELETE ON state_transitions BEGIN
                SELECT RAISE(ABORT,'state transitions are append-only'); END""",
            """CREATE TRIGGER terminal_attempts_no_update BEFORE UPDATE ON job_attempts
                WHEN OLD.state <> 'RUNNING' BEGIN SELECT RAISE(ABORT,'terminal attempts are immutable'); END""",
            """CREATE TRIGGER job_attempts_no_delete BEFORE DELETE ON job_attempts BEGIN
                SELECT RAISE(ABORT,'job attempts are append-only'); END""",
        ),
    ),
    Migration(
        version=5,
        name="005_human_gate_state_machine",
        statements=(
            "DROP TRIGGER state_transitions_no_update",
            "DROP TRIGGER state_transitions_no_delete",
            "DROP INDEX state_transitions_project_created",
            "DROP INDEX state_transitions_milestone_created",
            "DROP INDEX state_transitions_job_created",
            "ALTER TABLE state_transitions RENAME TO state_transitions_m9",
            """CREATE TABLE human_gates (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                milestone_id TEXT, gate_type TEXT NOT NULL CHECK(gate_type IN
                ('DESIGN_APPROVAL','PRODUCT_DECISION','TECHNICAL_DECISION','HUMAN_TEST',
                 'FINAL_ACCEPTANCE','RECOVERY_DECISION')),
                state TEXT NOT NULL CHECK(state IN
                ('PENDING','NOTIFIED','RESPONDED','VALIDATED','RESOLVED','EXPIRED','CANCELLED')),
                title TEXT NOT NULL, prompt TEXT NOT NULL,
                expected_response_type TEXT NOT NULL CHECK(expected_response_type IN
                ('DESIGN_APPROVAL','HUMAN_TEST','OPTION')),
                options_json TEXT NOT NULL, architect_recommendation TEXT,
                resume_project_state TEXT, resume_milestone_state TEXT,
                created_at TEXT NOT NULL, notified_at TEXT, responded_at TEXT, resolved_at TEXT,
                created_by TEXT NOT NULL, correlation_id TEXT NOT NULL, artifact_reference TEXT,
                FOREIGN KEY(project_id,milestone_id) REFERENCES milestones(project_id,id)
            ) STRICT""",
            "CREATE UNIQUE INDEX human_gates_project_identity ON human_gates(project_id,id)",
            """CREATE TABLE human_gate_responses (
                id TEXT PRIMARY KEY, gate_id TEXT NOT NULL REFERENCES human_gates(id),
                message_id TEXT NOT NULL UNIQUE, response_code TEXT NOT NULL,
                response_text TEXT, selected_option TEXT, attachments_json TEXT NOT NULL,
                responded_by TEXT NOT NULL, responded_at TEXT NOT NULL,
                validated INTEGER NOT NULL CHECK(validated IN (0,1)), validation_notes TEXT
            ) STRICT""",
            """CREATE TABLE state_transitions (
                id TEXT PRIMARY KEY, entity_type TEXT NOT NULL CHECK(entity_type IN
                ('PROJECT','MILESTONE','JOB','HUMAN_GATE')),
                entity_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
                milestone_id TEXT REFERENCES milestones(id), job_id TEXT REFERENCES jobs(id),
                gate_id TEXT REFERENCES human_gates(id), previous_state TEXT NOT NULL,
                new_state TEXT NOT NULL, reason TEXT NOT NULL, trigger_event_id TEXT,
                actor_type TEXT NOT NULL, actor_id TEXT, correlation_id TEXT NOT NULL,
                created_at TEXT NOT NULL, metadata_json TEXT,
                CHECK ((entity_type='PROJECT' AND entity_id=project_id AND milestone_id IS NULL AND job_id IS NULL AND gate_id IS NULL)
                    OR (entity_type='MILESTONE' AND entity_id=milestone_id AND milestone_id IS NOT NULL AND job_id IS NULL AND gate_id IS NULL)
                    OR (entity_type='JOB' AND entity_id=job_id AND job_id IS NOT NULL AND gate_id IS NULL)
                    OR (entity_type='HUMAN_GATE' AND entity_id=gate_id AND gate_id IS NOT NULL))
            ) STRICT""",
            """INSERT INTO state_transitions
                (id,entity_type,entity_id,project_id,milestone_id,job_id,gate_id,previous_state,new_state,
                 reason,trigger_event_id,actor_type,actor_id,correlation_id,created_at,metadata_json)
                SELECT id,entity_type,entity_id,project_id,milestone_id,job_id,NULL,previous_state,new_state,
                 reason,trigger_event_id,actor_type,actor_id,correlation_id,created_at,metadata_json
                FROM state_transitions_m9""",
            "DROP TABLE state_transitions_m9",
            "CREATE INDEX state_transitions_project_created ON state_transitions(project_id,created_at)",
            "CREATE INDEX state_transitions_milestone_created ON state_transitions(milestone_id,created_at)",
            "CREATE INDEX state_transitions_job_created ON state_transitions(job_id,created_at)",
            "CREATE INDEX state_transitions_gate_created ON state_transitions(gate_id,created_at)",
            "CREATE INDEX human_gates_outstanding ON human_gates(state,created_at)",
            """CREATE TRIGGER state_transitions_no_update BEFORE UPDATE ON state_transitions BEGIN
                SELECT RAISE(ABORT,'state transitions are append-only'); END""",
            """CREATE TRIGGER state_transitions_no_delete BEFORE DELETE ON state_transitions BEGIN
                SELECT RAISE(ABORT,'state transitions are append-only'); END""",
            """CREATE TRIGGER human_gate_responses_no_update BEFORE UPDATE ON human_gate_responses BEGIN
                SELECT RAISE(ABORT,'human gate responses are immutable'); END""",
            """CREATE TRIGGER human_gate_responses_no_delete BEFORE DELETE ON human_gate_responses BEGIN
                SELECT RAISE(ABORT,'human gate responses are append-only'); END""",
        ),
    ),
)


def validate_migrations(migrations: Sequence[Migration]) -> None:
    """Require positive, contiguous versions in their declared order."""
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError(
            "migration versions must be unique, contiguous, and ordered from 1"
        )
    if any(not item.name or not item.statements for item in migrations):
        raise MigrationError("each migration requires a name and SQL statements")


def _migration_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", _MIGRATION_TABLE),
    ).fetchone()
    return row is not None


def applied_migrations(connection: sqlite3.Connection) -> tuple[tuple[int, str], ...]:
    """Return ordered migration history, or an empty history initially."""
    if not _migration_table_exists(connection):
        return ()
    try:
        rows = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.Error as error:
        raise MigrationError("migration history could not be read") from error
    return tuple((int(row[0]), str(row[1])) for row in rows)


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Return the latest applied schema version, using zero for a new database."""
    history = applied_migrations(connection)
    return history[-1][0] if history else 0


def _validate_history(
    history: Sequence[tuple[int, str]], migrations: Sequence[Migration]
) -> None:
    if len(history) > len(migrations):
        raise MigrationError("database schema is newer than this application")
    expected = tuple((item.version, item.name) for item in migrations[: len(history)])
    if tuple(history) != expected:
        raise MigrationError("database migration history is unknown or inconsistent")


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> int:
    """Validate history and atomically apply each outstanding migration."""
    validate_migrations(migrations)
    history = applied_migrations(connection)
    _validate_history(history, migrations)

    for migration in migrations[len(history) :]:
        metadata = {"version": migration.version, "name": migration.name}
        _LOGGER.info(
            "Migration started",
            extra={"event": "migration_started", "metadata": metadata},
        )
        try:
            with transaction(connection):
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (migration.version, migration.name),
                )
        except Exception as error:
            raise MigrationError(
                f"migration {migration.version} could not be applied"
            ) from error
        _LOGGER.info(
            "Migration completed",
            extra={"event": "migration_completed", "metadata": metadata},
        )

    resulting_history = applied_migrations(connection)
    _validate_history(resulting_history, migrations)
    return current_schema_version(connection)
