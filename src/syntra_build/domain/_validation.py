"""Shared validation primitives for domain records."""

from datetime import datetime, timedelta
from enum import Enum

from syntra_build.domain.errors import DomainValidationError


def require_text(value: object, field_name: str) -> str:
    """Return a non-blank string or raise a domain validation error."""
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string")
    return value


def require_utc(value: object, field_name: str) -> datetime:
    """Return a timezone-aware UTC datetime without silently converting it."""
    if not isinstance(value, datetime):
        raise DomainValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise DomainValidationError(f"{field_name} must be in UTC")
    return value


def require_enum[EnumT: Enum](
    value: object, enum_type: type[EnumT], field_name: str
) -> EnumT:
    """Require an actual member of the declared domain enum."""
    if not isinstance(value, enum_type):
        raise DomainValidationError(f"{field_name} must be a {enum_type.__name__}")
    return value


def require_identifier(
    value: object, identifier_type: type[object], field_name: str
) -> None:
    """Require the correct identifier subtype at aggregate boundaries."""
    if not isinstance(value, identifier_type):
        raise DomainValidationError(
            f"{field_name} must be a {identifier_type.__name__}"
        )


def require_timestamp_order(
    earlier: datetime, later: datetime, earlier_name: str, later_name: str
) -> None:
    """Reject a later lifecycle timestamp that predates its origin."""
    if later < earlier:
        raise DomainValidationError(f"{later_name} cannot precede {earlier_name}")
