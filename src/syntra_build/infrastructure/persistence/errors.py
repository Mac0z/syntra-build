"""Errors raised by the SQLite persistence boundary."""


class PersistenceError(RuntimeError):
    """Base class for database failures exposed to the application."""


class DatabaseConnectionError(PersistenceError):
    """Raised when a database cannot be opened or configured safely."""


class TransactionError(PersistenceError):
    """Raised when transaction usage violates the persistence contract."""


class MigrationError(PersistenceError):
    """Raised when migration state is invalid or a migration fails."""


class DatabaseIntegrityError(PersistenceError):
    """Raised when SQLite reports an integrity problem."""


class StaleProjectStateError(PersistenceError):
    """The project no longer has the caller's expected state."""


class StaleMilestoneStateError(PersistenceError):
    """The milestone no longer has the caller's expected state."""


class MilestoneProjectMismatchError(PersistenceError):
    """A milestone operation named a project other than its owner."""


class ActiveMilestoneConflictError(PersistenceError):
    """A project already has an active implementation milestone."""


class UnsatisfiedMilestoneDependenciesError(PersistenceError):
    """A milestone cannot activate until every dependency is complete."""


class MilestoneDependencyError(PersistenceError):
    """A milestone dependency is invalid or could not be stored."""


class StaleJobStateError(PersistenceError):
    """The job no longer has the caller's expected state."""


class JobProjectMismatchError(PersistenceError):
    """A job operation named a project other than its owner."""


class JobMilestoneProjectMismatchError(PersistenceError):
    """A job's milestone belongs to another project."""


class TerminalJobMutationError(PersistenceError):
    """An operation attempted to mutate a terminal job."""


class AttemptLimitExhaustedError(PersistenceError):
    """A job cannot begin another configured attempt."""


class InvalidAttemptError(PersistenceError):
    """An attempt number or lifecycle operation is invalid."""


class ImmutableTerminalAttemptError(PersistenceError):
    """A terminal attempt cannot be changed."""
