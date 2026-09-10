"""SQLite persistence bootstrap and explicit connection ownership."""

from __future__ import annotations

import logging
import sqlite3

from syntra_build.domain import ProjectCreationContext
from syntra_build.infrastructure.config import ApplicationConfig
from syntra_build.infrastructure.persistence.architect import (
    SQLiteArchitectInteractionRepository,
)
from syntra_build.infrastructure.persistence.connection import (
    BUSY_TIMEOUT_MILLISECONDS,
    open_database,
    transaction,
    transaction_scope,
)
from syntra_build.infrastructure.persistence.cursors import (
    SQLiteProviderCursorRepository,
)
from syntra_build.infrastructure.persistence.design import (
    SQLiteDesignMessageRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    document_content_hash,
)
from syntra_build.infrastructure.persistence.design_packages import (
    SQLiteDesignPackageRepository,
)
from syntra_build.infrastructure.persistence.errors import (
    ActiveMilestoneConflictError,
    AttemptLimitExhaustedError,
    DatabaseConnectionError,
    DatabaseIntegrityError,
    EventAlreadyProcessedError,
    EventClaimConflictError,
    EventNotFoundError,
    EventParentMismatchError,
    ImmutableTerminalAttemptError,
    InvalidAttemptError,
    InvalidEventCausationError,
    JobMilestoneProjectMismatchError,
    JobProjectMismatchError,
    JobProjectStateIneligibleError,
    MigrationError,
    MilestoneDependencyError,
    MilestoneProjectMismatchError,
    PersistenceError,
    StaleJobStateError,
    StaleMilestoneStateError,
    StaleProjectStateError,
    TerminalJobMutationError,
    TransactionError,
    UnsatisfiedMilestoneDependenciesError,
)
from syntra_build.infrastructure.persistence.events import SQLiteWorkflowEventRepository
from syntra_build.infrastructure.persistence.gates import SQLiteHumanGateRepository
from syntra_build.infrastructure.persistence.jobs import (
    JobStateTransition,
    SchedulableJob,
    SQLiteJobRepository,
)
from syntra_build.infrastructure.persistence.migrations import (
    MIGRATIONS,
    Migration,
    apply_migrations,
    current_schema_version,
    validate_migrations,
)
from syntra_build.infrastructure.persistence.milestones import (
    MilestoneStateTransition,
    SQLiteMilestoneRepository,
)
from syntra_build.infrastructure.persistence.projects import (
    ProjectStateTransition,
    SQLiteProjectRepository,
)
from syntra_build.infrastructure.persistence.telegram_interactions import (
    SQLiteTelegramGateInteractionRepository,
    TelegramGateInteraction,
    TelegramInteractionState,
)

_LOGGER = logging.getLogger(__name__)


def validate_integrity_results(results: list[str]) -> None:
    """Validate the deterministic result returned by SQLite ``quick_check``."""
    if results != ["ok"]:
        raise DatabaseIntegrityError("database quick integrity check failed")


def check_database_integrity(connection: sqlite3.Connection) -> None:
    """Run SQLite's lightweight startup integrity validation."""
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as error:
        raise DatabaseIntegrityError(
            "database integrity could not be checked"
        ) from error
    validate_integrity_results([str(row[0]).casefold() for row in rows])
    _LOGGER.info(
        "Database integrity checked", extra={"event": "database_integrity_checked"}
    )


def bootstrap_database(config: ApplicationConfig) -> sqlite3.Connection:
    """Open, migrate, validate, and return a caller-owned database connection."""
    connection = open_database(config.database.sqlite_path)
    try:
        apply_migrations(connection)
        check_database_integrity(connection)
    except BaseException:
        connection.close()
        raise
    return connection


__all__ = [
    "BUSY_TIMEOUT_MILLISECONDS",
    "ActiveMilestoneConflictError",
    "MIGRATIONS",
    "DatabaseConnectionError",
    "DatabaseIntegrityError",
    "Migration",
    "MigrationError",
    "PersistenceError",
    "TransactionError",
    "StaleProjectStateError",
    "StaleMilestoneStateError",
    "MilestoneDependencyError",
    "MilestoneProjectMismatchError",
    "UnsatisfiedMilestoneDependenciesError",
    "MilestoneStateTransition",
    "SQLiteMilestoneRepository",
    "SQLiteJobRepository",
    "SQLiteHumanGateRepository",
    "SQLiteWorkflowEventRepository",
    "SQLiteTelegramGateInteractionRepository",
    "TelegramGateInteraction",
    "TelegramInteractionState",
    "SQLiteProviderCursorRepository",
    "SQLiteArchitectInteractionRepository",
    "SQLiteDesignMessageRepository",
    "SQLiteProjectDecisionRepository",
    "SQLiteProjectDocumentRepository",
    "SQLiteDesignPackageRepository",
    "document_content_hash",
    "JobStateTransition",
    "SchedulableJob",
    "StaleJobStateError",
    "JobProjectMismatchError",
    "JobProjectStateIneligibleError",
    "JobMilestoneProjectMismatchError",
    "TerminalJobMutationError",
    "AttemptLimitExhaustedError",
    "InvalidAttemptError",
    "ImmutableTerminalAttemptError",
    "ProjectStateTransition",
    "ProjectCreationContext",
    "SQLiteProjectRepository",
    "apply_migrations",
    "bootstrap_database",
    "check_database_integrity",
    "current_schema_version",
    "open_database",
    "transaction",
    "transaction_scope",
    "EventAlreadyProcessedError",
    "EventClaimConflictError",
    "EventNotFoundError",
    "EventParentMismatchError",
    "InvalidEventCausationError",
    "validate_integrity_results",
    "validate_migrations",
]
