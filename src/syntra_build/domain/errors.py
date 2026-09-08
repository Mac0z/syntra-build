"""Errors raised when values violate domain invariants."""


class DomainValidationError(ValueError):
    """A supplied value cannot form a valid Syntra domain record."""


class InvalidProjectTransitionError(ValueError):
    """A requested project transition violates the lifecycle policy."""


class InvalidMilestoneTransitionError(ValueError):
    """A requested milestone transition violates the lifecycle policy."""


class InvalidBlockedRecoveryError(InvalidMilestoneTransitionError):
    """A blocked milestone was not restored to its persisted recovery target."""


class InvalidJobTransitionError(ValueError):
    """A requested job transition violates the lifecycle policy."""


class InvalidRetryMetadataError(InvalidJobTransitionError):
    """Retry state lacks a future attempt or durable failure metadata."""
