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
    Migration(
        version=6,
        name="006_workflow_event_framework",
        statements=(
            "CREATE UNIQUE INDEX jobs_project_identity ON jobs(project_id,id)",
            """CREATE TABLE workflow_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN
                    ('INTERNAL','TELEGRAM','GITHUB','CI','ARCHITECT','CODEX','RECOVERY','SYSTEM')),
                correlation_id TEXT NOT NULL,
                causation_event_id TEXT,
                external_deduplication_key TEXT,
                project_id TEXT NOT NULL REFERENCES projects(id),
                milestone_id TEXT,
                job_id TEXT,
                gate_id TEXT,
                occurred_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
                processing_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(processing_status IN
                    ('PENDING','PROCESSED','REJECTED','FAILED')),
                processing_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(processing_attempt_count >= 0),
                processed_at TEXT,
                last_processing_error TEXT,
                processing_claim_token TEXT,
                processing_claimed_at TEXT,
                UNIQUE(project_id,id),
                FOREIGN KEY(project_id,milestone_id) REFERENCES milestones(project_id,id),
                FOREIGN KEY(project_id,job_id) REFERENCES jobs(project_id,id),
                FOREIGN KEY(project_id,gate_id) REFERENCES human_gates(project_id,id),
                FOREIGN KEY(project_id,causation_event_id) REFERENCES workflow_events(project_id,id),
                CHECK(causation_event_id IS NULL OR causation_event_id <> id),
                CHECK((source IN ('INTERNAL','RECOVERY','SYSTEM'))
                    OR external_deduplication_key IS NOT NULL),
                CHECK((processing_status IN ('PROCESSED','REJECTED') AND processed_at IS NOT NULL)
                    OR (processing_status IN ('PENDING','FAILED') AND processed_at IS NULL))
            ) STRICT""",
            """CREATE UNIQUE INDEX workflow_events_external_deduplication
                ON workflow_events(external_deduplication_key)
                WHERE external_deduplication_key IS NOT NULL""",
            "CREATE INDEX workflow_events_processing ON workflow_events(processing_status,received_at,id)",
            "CREATE INDEX workflow_events_project ON workflow_events(project_id,received_at,id)",
            "CREATE INDEX workflow_events_correlation ON workflow_events(correlation_id,received_at,id)",
            """CREATE TRIGGER workflow_events_immutable_envelope BEFORE UPDATE ON workflow_events
                WHEN NEW.id<>OLD.id OR NEW.event_type<>OLD.event_type OR NEW.source<>OLD.source
                  OR NEW.correlation_id<>OLD.correlation_id
                  OR NEW.causation_event_id IS NOT OLD.causation_event_id
                  OR NEW.external_deduplication_key IS NOT OLD.external_deduplication_key
                  OR NEW.project_id<>OLD.project_id OR NEW.milestone_id IS NOT OLD.milestone_id
                  OR NEW.job_id IS NOT OLD.job_id OR NEW.gate_id IS NOT OLD.gate_id
                  OR NEW.occurred_at<>OLD.occurred_at OR NEW.received_at<>OLD.received_at
                  OR NEW.payload_json<>OLD.payload_json
                BEGIN SELECT RAISE(ABORT,'workflow event envelope is immutable'); END""",
            """CREATE TRIGGER workflow_events_no_delete BEFORE DELETE ON workflow_events BEGIN
                SELECT RAISE(ABORT,'workflow events are append-only'); END""",
            """CREATE TRIGGER workflow_events_job_milestone_consistency BEFORE INSERT ON workflow_events
                WHEN NEW.job_id IS NOT NULL AND NEW.milestone_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM jobs WHERE id=NEW.job_id
                    AND project_id=NEW.project_id AND milestone_id=NEW.milestone_id)
                BEGIN SELECT RAISE(ABORT,'event job and milestone are inconsistent'); END""",
            """CREATE TRIGGER workflow_events_gate_milestone_consistency BEFORE INSERT ON workflow_events
                WHEN NEW.gate_id IS NOT NULL AND NEW.milestone_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM human_gates WHERE id=NEW.gate_id
                    AND project_id=NEW.project_id AND milestone_id=NEW.milestone_id)
                BEGIN SELECT RAISE(ABORT,'event gate and milestone are inconsistent'); END""",
        ),
    ),
    Migration(
        version=7,
        name="007_retry_backoff_fairness",
        statements=(
            "ALTER TABLE jobs ADD COLUMN failure_classification TEXT CHECK(failure_classification IS NULL OR failure_classification IN ('TRANSIENT','PERMANENT','POLICY','CANCELLED','UNKNOWN'))",
            "ALTER TABLE jobs ADD COLUMN retry_exhausted INTEGER NOT NULL DEFAULT 0 CHECK(retry_exhausted IN (0,1))",
            "ALTER TABLE jobs ADD COLUMN exhaustion_reason TEXT",
            "ALTER TABLE milestones ADD COLUMN codex_cycle_count INTEGER NOT NULL DEFAULT 0 CHECK(codex_cycle_count >= 0)",
            "ALTER TABLE milestones ADD COLUMN ci_rework_count INTEGER NOT NULL DEFAULT 0 CHECK(ci_rework_count >= 0)",
            "ALTER TABLE milestones ADD COLUMN architect_rework_count INTEGER NOT NULL DEFAULT 0 CHECK(architect_rework_count >= 0)",
            "ALTER TABLE milestones ADD COLUMN human_test_rework_count INTEGER NOT NULL DEFAULT 0 CHECK(human_test_rework_count >= 0)",
            "ALTER TABLE milestones ADD COLUMN exhaustion_reason TEXT",
            "CREATE INDEX jobs_retry_due ON jobs(state,next_retry_at,id)",
        ),
    ),
    Migration(
        version=8,
        name="008_project_creation",
        statements=(
            "ALTER TABLE projects ADD COLUMN canonical_name TEXT",
            "CREATE UNIQUE INDEX projects_canonical_name_unique ON projects(canonical_name) WHERE canonical_name IS NOT NULL",
            """CREATE TABLE project_creation_context (
                project_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                initial_request TEXT NOT NULL,
                messaging_platform TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                thread_id TEXT,
                source_update_id TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            ) STRICT""",
        ),
    ),
    Migration(
        version=9,
        name="009_provider_cursors",
        statements=(
            """CREATE TABLE provider_cursors (
                provider TEXT PRIMARY KEY,
                last_processed_update_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT""",
        ),
    ),
    Migration(
        version=10,
        name="010_design_persistence",
        statements=(
            """CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                milestone_id TEXT REFERENCES milestones(id),
                gate_id TEXT REFERENCES human_gates(id),
                direction TEXT NOT NULL CHECK(direction IN ('INBOUND','OUTBOUND')),
                platform TEXT NOT NULL,
                external_message_id TEXT,
                chat_id TEXT NOT NULL,
                thread_id TEXT,
                sender_id TEXT,
                message_type TEXT NOT NULL,
                text TEXT,
                attachments_json TEXT,
                reply_to_message_id TEXT,
                received_at TEXT,
                sent_at TEXT,
                correlation_id TEXT NOT NULL,
                raw_metadata_json TEXT,
                CHECK((direction='INBOUND' AND received_at IS NOT NULL AND sent_at IS NULL)
                   OR (direction='OUTBOUND' AND sent_at IS NOT NULL AND received_at IS NULL))
            ) STRICT""",
            """CREATE UNIQUE INDEX messages_external_delivery_unique
                ON messages(platform,chat_id,external_message_id)
                WHERE external_message_id IS NOT NULL""",
            """CREATE INDEX messages_project_chronological ON messages(
                project_id,coalesce(received_at,sent_at),id)""",
            """CREATE TRIGGER messages_parent_consistency BEFORE INSERT ON messages
                WHEN (NEW.milestone_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM milestones WHERE id=NEW.milestone_id
                    AND project_id=NEW.project_id))
                  OR (NEW.gate_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM human_gates WHERE id=NEW.gate_id
                    AND project_id=NEW.project_id))
                BEGIN SELECT RAISE(ABORT,'message parent belongs to another project'); END""",
            """CREATE TRIGGER messages_no_update BEFORE UPDATE ON messages BEGIN
                SELECT RAISE(ABORT,'messages are append-only'); END""",
            """CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages BEGIN
                SELECT RAISE(ABORT,'messages are append-only'); END""",
            """CREATE TABLE project_documents (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                document_type TEXT NOT NULL CHECK(document_type IN ('SPEC','AGENTS')),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                status TEXT NOT NULL CHECK(status IN
                    ('DRAFT','APPROVED','SUPERSEDED','REJECTED')),
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                approved_at TEXT,
                approved_by TEXT,
                supersedes_document_id TEXT REFERENCES project_documents(id),
                UNIQUE(project_id,document_type,revision),
                CHECK((status='APPROVED' AND approved_at IS NOT NULL AND approved_by IS NOT NULL)
                    OR status<>'APPROVED'),
                CHECK(supersedes_document_id IS NULL
                    OR status IN ('APPROVED','SUPERSEDED')),
                CHECK(supersedes_document_id IS NULL OR supersedes_document_id<>id)
            ) STRICT""",
            """CREATE UNIQUE INDEX project_documents_one_approved
                ON project_documents(project_id,document_type)
                WHERE status='APPROVED'""",
            """CREATE INDEX project_documents_project_current
                ON project_documents(project_id,document_type,revision DESC)""",
            """CREATE TRIGGER project_documents_content_immutable
                BEFORE UPDATE OF content,content_hash,project_id,document_type,revision
                ON project_documents BEGIN
                SELECT RAISE(ABORT,'document revision content and identity are immutable'); END""",
            """CREATE TRIGGER project_documents_no_delete BEFORE DELETE
                ON project_documents BEGIN
                SELECT RAISE(ABORT,'document revisions are preservation-oriented'); END""",
            """CREATE TRIGGER project_documents_supersedes_consistency
                BEFORE INSERT ON project_documents
                WHEN NEW.supersedes_document_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM project_documents WHERE id=NEW.supersedes_document_id
                    AND project_id=NEW.project_id
                    AND document_type=NEW.document_type
                    AND revision<NEW.revision)
                BEGIN SELECT RAISE(ABORT,'superseded document is inconsistent'); END""",
            """CREATE TRIGGER project_documents_supersedes_update_consistency
                BEFORE UPDATE OF supersedes_document_id ON project_documents
                WHEN NEW.supersedes_document_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM project_documents WHERE id=NEW.supersedes_document_id
                    AND project_id=NEW.project_id
                    AND document_type=NEW.document_type
                    AND revision<NEW.revision)
                BEGIN SELECT RAISE(ABORT,'superseded document is inconsistent'); END""",
            """CREATE TRIGGER project_documents_lineage_set_on_approval
                BEFORE UPDATE OF supersedes_document_id ON project_documents
                WHEN OLD.status<>'DRAFT' OR NEW.status<>'APPROVED'
                BEGIN SELECT RAISE(ABORT,'document lineage is set only on approval'); END""",
            """CREATE TABLE project_decisions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                milestone_id TEXT REFERENCES milestones(id),
                decision_type TEXT NOT NULL,
                title TEXT NOT NULL,
                decision TEXT NOT NULL,
                rationale TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('HUMAN','ARCHITECT','POLICY','SYSTEM')),
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                superseded_by_decision_id TEXT REFERENCES project_decisions(id),
                CHECK(superseded_by_decision_id IS NULL OR superseded_by_decision_id<>id)
            ) STRICT""",
            """CREATE INDEX project_decisions_project_chronological
                ON project_decisions(project_id,created_at,id)""",
            """CREATE INDEX project_decisions_project_active
                ON project_decisions(project_id,decision_type,created_at,id)
                WHERE superseded_by_decision_id IS NULL""",
            """CREATE TRIGGER project_decisions_parent_consistency
                BEFORE INSERT ON project_decisions
                WHEN NEW.milestone_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM milestones WHERE id=NEW.milestone_id
                    AND project_id=NEW.project_id)
                BEGIN SELECT RAISE(ABORT,'decision milestone belongs to another project'); END""",
            """CREATE TRIGGER project_decisions_supersession_consistency
                BEFORE UPDATE OF superseded_by_decision_id ON project_decisions
                WHEN NEW.superseded_by_decision_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM project_decisions WHERE id=NEW.superseded_by_decision_id
                    AND project_id=NEW.project_id)
                BEGIN SELECT RAISE(ABORT,'superseding decision belongs to another project'); END""",
            """CREATE TRIGGER project_decisions_no_delete BEFORE DELETE
                ON project_decisions BEGIN
                SELECT RAISE(ABORT,'project decisions are preservation-oriented'); END""",
        ),
    ),
    Migration(
        version=11,
        name="011_architect_adapter",
        statements=(
            """CREATE TABLE architect_sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), purpose TEXT NOT NULL CHECK(purpose='DESIGN'), provider TEXT NOT NULL, model TEXT NOT NULL, external_session_id TEXT, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL, status TEXT NOT NULL, metadata_json TEXT) STRICT""",
            """CREATE TABLE architect_requests (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), session_id TEXT REFERENCES architect_sessions(id), request_type TEXT NOT NULL CHECK(request_type='DESIGN'), provider TEXT NOT NULL, model TEXT NOT NULL, reasoning_level TEXT NOT NULL, request_schema_version TEXT NOT NULL, request_payload_json TEXT NOT NULL, correlation_id TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, external_request_id TEXT, status TEXT NOT NULL CHECK(status IN ('STARTED','SUCCEEDED','FAILED')), failure_classification TEXT, UNIQUE(project_id,correlation_id,id)) STRICT""",
            "CREATE INDEX architect_requests_project_created ON architect_requests(project_id,started_at,id)",
            """CREATE TABLE architect_responses (id TEXT PRIMARY KEY, architect_request_id TEXT NOT NULL UNIQUE REFERENCES architect_requests(id), response_type TEXT NOT NULL CHECK(response_type='DESIGN'), response_schema_version TEXT NOT NULL, normalised_payload_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status='ACCEPTED'), created_at TEXT NOT NULL, validation_status TEXT NOT NULL CHECK(validation_status='VALID'), provider TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER, CHECK(input_tokens IS NULL OR input_tokens>=0), CHECK(cached_input_tokens IS NULL OR cached_input_tokens>=0), CHECK(output_tokens IS NULL OR output_tokens>=0), CHECK(reasoning_tokens IS NULL OR reasoning_tokens>=0), CHECK(total_tokens IS NULL OR total_tokens>=0)) STRICT""",
            """CREATE TRIGGER architect_responses_no_update BEFORE UPDATE ON architect_responses BEGIN SELECT RAISE(ABORT,'architect responses are append-only'); END""",
            """CREATE TRIGGER architect_responses_no_delete BEFORE DELETE ON architect_responses BEGIN SELECT RAISE(ABORT,'architect responses are append-only'); END""",
        ),
    ),
    Migration(
        version=12,
        name="012_design_packages",
        statements=(
            "ALTER TABLE projects ADD COLUMN repository_visibility TEXT NOT NULL DEFAULT 'public' CHECK(repository_visibility IN ('public','private'))",
            "DROP TRIGGER architect_responses_no_update",
            "DROP TRIGGER architect_responses_no_delete",
            "ALTER TABLE architect_responses RENAME TO architect_responses_m16",
            "ALTER TABLE architect_requests RENAME TO architect_requests_m16",
            """CREATE TABLE architect_requests (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), session_id TEXT REFERENCES architect_sessions(id), request_type TEXT NOT NULL CHECK(request_type IN ('DESIGN','SPECIFICATION_DRAFT')), provider TEXT NOT NULL, model TEXT NOT NULL, reasoning_level TEXT NOT NULL, request_schema_version TEXT NOT NULL, request_payload_json TEXT NOT NULL, correlation_id TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, external_request_id TEXT, status TEXT NOT NULL CHECK(status IN ('STARTED','SUCCEEDED','FAILED')), failure_classification TEXT, UNIQUE(project_id,correlation_id,id)) STRICT""",
            """INSERT INTO architect_requests SELECT * FROM architect_requests_m16""",
            """CREATE TABLE architect_responses (id TEXT PRIMARY KEY, architect_request_id TEXT NOT NULL UNIQUE REFERENCES architect_requests(id), response_type TEXT NOT NULL CHECK(response_type IN ('DESIGN','SPECIFICATION_DRAFT')), response_schema_version TEXT NOT NULL, normalised_payload_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status='ACCEPTED'), created_at TEXT NOT NULL, validation_status TEXT NOT NULL CHECK(validation_status='VALID'), provider TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER, CHECK(input_tokens IS NULL OR input_tokens>=0), CHECK(cached_input_tokens IS NULL OR cached_input_tokens>=0), CHECK(output_tokens IS NULL OR output_tokens>=0), CHECK(reasoning_tokens IS NULL OR reasoning_tokens>=0), CHECK(total_tokens IS NULL OR total_tokens>=0)) STRICT""",
            "INSERT INTO architect_responses SELECT * FROM architect_responses_m16",
            "DROP TABLE architect_responses_m16",
            "DROP TABLE architect_requests_m16",
            "CREATE INDEX architect_requests_project_created ON architect_requests(project_id,started_at,id)",
            "CREATE TRIGGER architect_responses_no_update BEFORE UPDATE ON architect_responses BEGIN SELECT RAISE(ABORT,'architect responses are append-only'); END",
            "CREATE TRIGGER architect_responses_no_delete BEFORE DELETE ON architect_responses BEGIN SELECT RAISE(ABORT,'architect responses are append-only'); END",
            """CREATE TABLE design_packages (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), architect_request_id TEXT NOT NULL REFERENCES architect_requests(id), spec_document_id TEXT NOT NULL REFERENCES project_documents(id), agents_document_id TEXT NOT NULL REFERENCES project_documents(id), repository_visibility TEXT NOT NULL CHECK(repository_visibility IN ('public','private')), design_summary TEXT NOT NULL CHECK(length(trim(design_summary))>0), planned_milestones_json TEXT NOT NULL, assumptions_json TEXT NOT NULL, non_blocking_issues_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING_APPROVAL','APPROVED','REJECTED')), approval_gate_id TEXT NOT NULL UNIQUE REFERENCES human_gates(id), created_at TEXT NOT NULL, approved_at TEXT, approved_by TEXT, rejected_at TEXT, rejection_feedback TEXT, CHECK((status='APPROVED' AND approved_at IS NOT NULL AND approved_by IS NOT NULL AND rejected_at IS NULL) OR (status='REJECTED' AND rejected_at IS NOT NULL AND approved_at IS NULL) OR status='PENDING_APPROVAL')) STRICT""",
            "CREATE INDEX design_packages_project_status ON design_packages(project_id,status,created_at)",
            """CREATE TRIGGER design_packages_document_consistency BEFORE INSERT ON design_packages WHEN NOT EXISTS (SELECT 1 FROM project_documents WHERE id=NEW.spec_document_id AND project_id=NEW.project_id AND document_type='SPEC' AND status='DRAFT') OR NOT EXISTS (SELECT 1 FROM project_documents WHERE id=NEW.agents_document_id AND project_id=NEW.project_id AND document_type='AGENTS' AND status='DRAFT') BEGIN SELECT RAISE(ABORT,'design package documents are inconsistent'); END""",
            """CREATE TRIGGER design_packages_identity_immutable BEFORE UPDATE OF project_id,architect_request_id,spec_document_id,agents_document_id,repository_visibility,design_summary,planned_milestones_json,assumptions_json,non_blocking_issues_json,approval_gate_id,created_at ON design_packages BEGIN SELECT RAISE(ABORT,'design package identity is immutable'); END""",
            """CREATE TABLE design_change_feedback (id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES design_packages(id), project_id TEXT NOT NULL REFERENCES projects(id), feedback TEXT NOT NULL CHECK(length(trim(feedback))>0), provided_by TEXT NOT NULL, created_at TEXT NOT NULL) STRICT""",
            "CREATE INDEX design_feedback_project_created ON design_change_feedback(project_id,created_at,id)",
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
