"""Provider-independent contracts for M21 worktree validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from syntra_build.domain.identifiers import MilestoneId, ProjectId


class FindingSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class FindingCode(StrEnum):
    SECRET_DETECTED = "SECRET_DETECTED"
    WORKSPACE_ESCAPE_ATTEMPT = "WORKSPACE_ESCAPE_ATTEMPT"
    PROTECTED_PATH_CHANGE = "PROTECTED_PATH_CHANGE"
    REPOSITORY_IDENTITY_MISMATCH = "REPOSITORY_IDENTITY_MISMATCH"
    UNEXPECTED_GIT_HISTORY_CHANGE = "UNEXPECTED_GIT_HISTORY_CHANGE"
    EMPTY_CHANGE_SET = "EMPTY_CHANGE_SET"
    BINARY_NOT_SCANNED = "BINARY_NOT_SCANNED"
    TEXT_SCAN_LIMIT_EXCEEDED = "TEXT_SCAN_LIMIT_EXCEEDED"


class ValidationDecision(StrEnum):
    ACCEPT = "ACCEPT"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    status: str
    staged: bool
    binary: bool
    content_hash: str | None
    file_kind: str
    mode: str | None
    lines_added: int | None = None
    lines_deleted: int | None = None


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    id: str
    change_set_id: str
    code: FindingCode
    severity: FindingSeverity
    path: str | None
    location: str | None
    fingerprint: str | None
    message: str
    remediation: str
    blocking: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChangeSet:
    id: str
    interface_version: str
    project_id: ProjectId
    milestone_id: MilestoneId
    workspace_id: str
    branch_name: str
    base_sha: str
    trusted_head_sha: str
    files: tuple[ChangedFile, ...]
    diff_hash: str
    is_empty: bool
    findings: tuple[ValidationFinding, ...]
    decision: ValidationDecision
    correlation_id: str
    scanner_version: str
    policy_version: str
    created_at: datetime

    @property
    def changed_files(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if item.status == "MODIFIED")

    @property
    def added_files(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if item.status == "ADDED")

    @property
    def deleted_files(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if item.status == "DELETED")

    @property
    def staged_files(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if item.staged)
