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
