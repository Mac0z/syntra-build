"""Provider-neutral, bounded security audit contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from syntra_build.domain.identifiers import MilestoneId, ProjectId


class SecuritySeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class SecurityEventType(StrEnum):
    UNAUTHORISED_MESSAGE = "UNAUTHORISED_MESSAGE"
    SECRET_DETECTED = "SECRET_DETECTED"
    WORKSPACE_ESCAPE_ATTEMPT = "WORKSPACE_ESCAPE_ATTEMPT"
    PROTECTED_PATH_CHANGE = "PROTECTED_PATH_CHANGE"
    REPOSITORY_IDENTITY_MISMATCH = "REPOSITORY_IDENTITY_MISMATCH"
    UNEXPECTED_GIT_HISTORY_CHANGE = "UNEXPECTED_GIT_HISTORY_CHANGE"
    TEXT_SCAN_LIMIT_EXCEEDED = "TEXT_SCAN_LIMIT_EXCEEDED"
    PR_SHA_MISMATCH = "PR_SHA_MISMATCH"
    UNAUTHORISED_GATE_RESPONSE = "UNAUTHORISED_GATE_RESPONSE"
    GITHUB_CREDENTIAL_ERROR = "GITHUB_CREDENTIAL_ERROR"
    DATABASE_INTEGRITY_FAILURE = "DATABASE_INTEGRITY_FAILURE"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"


class SecurityResolutionCode(StrEnum):
    CLEAN_REVALIDATION = "CLEAN_REVALIDATION"


class SecurityActorType(StrEnum):
    SYSTEM = "SYSTEM"
    OPERATOR = "OPERATOR"


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    id: str
    event_type: SecurityEventType
    severity: SecuritySeverity
    project_id: ProjectId | None
    milestone_id: MilestoneId | None
    source_component: str
    source_reference: str | None
    correlation_id: str
    safe_details: Mapping[str, str | int | bool | None]
    blocking: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SecurityEventResolution:
    id: str
    security_event_id: str
    resolution_code: SecurityResolutionCode
    correlation_id: str
    actor_type: SecurityActorType
    actor_id: str | None
    resolved_at: datetime
