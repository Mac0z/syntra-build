"""Errors raised when values violate domain invariants."""


class DomainValidationError(ValueError):
    """A supplied value cannot form a valid Syntra domain record."""


class InvalidProjectTransitionError(ValueError):
    """A requested project transition violates the lifecycle policy."""
