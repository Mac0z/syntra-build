"""Errors raised when values violate domain invariants."""


class DomainValidationError(ValueError):
    """A supplied value cannot form a valid Syntra domain record."""
