"""Provider-neutral contracts for deterministic merge policy and execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import MilestoneId, ProjectId

MERGE_INTERFACE_VERSION = "1.0"


class MergeStrategy(StrEnum):
    SQUASH = "SQUASH"
    MERGE = "MERGE"
    REBASE = "REBASE"


class MergeStatus(StrEnum):
    MERGED = "MERGED"
    NOT_MERGED = "NOT_MERGED"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class MergeEligibilityRequest:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    github_repository_id: str
    repository_id: int
    pull_request_id: str
    pull_request_number: int
    expected_head_branch: str
    expected_base_branch: str
    expected_head_sha: str

    def __post_init__(self) -> None:
        if self.interface_version != MERGE_INTERFACE_VERSION:
            raise DomainValidationError("unsupported merge interface_version")
        require_text(self.correlation_id, "correlation_id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_text(self.github_repository_id, "github_repository_id")
        require_text(self.pull_request_id, "pull_request_id")
        require_text(self.expected_head_branch, "expected_head_branch")
        require_text(self.expected_base_branch, "expected_base_branch")
        if not isinstance(self.repository_id, int) or self.repository_id <= 0:
            raise DomainValidationError("repository_id must be positive")
        if (
            not isinstance(self.pull_request_number, int)
            or self.pull_request_number <= 0
        ):
            raise DomainValidationError("pull_request_number must be positive")
        if (
            not isinstance(self.expected_head_sha, str)
            or len(self.expected_head_sha) != 40
        ):
            raise DomainValidationError("expected_head_sha must be a 40-character SHA")


@dataclass(frozen=True, slots=True)
class MergeGuardResult:
    guard: str
    passed: bool
    reason: str

    def __post_init__(self) -> None:
        require_text(self.guard, "guard")
        require_text(self.reason, "reason")
        if not isinstance(self.passed, bool):
            raise DomainValidationError("passed must be boolean")


@dataclass(frozen=True, slots=True)
class MergeEligibilityResult:
    interface_version: str
    result_id: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    repository_id: int
    pull_request_number: int
    head_sha: str
    eligible: bool
    guards: tuple[MergeGuardResult, ...]
    evidence: MergeEvidence
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if self.interface_version != MERGE_INTERFACE_VERSION:
            raise DomainValidationError("unsupported merge interface_version")
        require_text(self.result_id, "result_id")
        require_text(self.correlation_id, "correlation_id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_utc(self.evaluated_at, "evaluated_at")
        if self.repository_id <= 0 or self.pull_request_number <= 0:
            raise DomainValidationError("evaluated identities must be positive")
        if len(self.head_sha) != 40:
            raise DomainValidationError("head_sha must be a 40-character SHA")
        if not isinstance(self.eligible, bool) or not self.guards:
            raise DomainValidationError(
                "eligibility and individual guards are required"
            )
        if self.eligible != all(item.passed for item in self.guards):
            raise DomainValidationError(
                "eligible must equal the combined guard results"
            )


@dataclass(frozen=True, slots=True)
class MergeEvidence:
    """Stable identifiers for the trusted records used by one evaluation."""

    github_repository_id: str | None
    pull_request_id: str | None
    ci_run_id: str | None
    architect_review_id: str | None
    human_gate_ids: tuple[str, ...] = ()
    human_response_ids: tuple[str, ...] = ()
    human_test_gate_id: str | None = None
    human_test_result_id: str | None = None
    human_test_ci_run_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "github_repository_id",
            "pull_request_id",
            "ci_run_id",
            "architect_review_id",
            "human_test_gate_id",
            "human_test_result_id",
            "human_test_ci_run_id",
        ):
            value = getattr(self, name)
            if value is not None:
                require_text(value, name)
        for value in (*self.human_gate_ids, *self.human_response_ids):
            require_text(value, "evidence identifier")


@dataclass(frozen=True, slots=True)
class MergeRequest:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    repository_id: int
    pull_request_number: int
    expected_head_sha: str
    merge_strategy: MergeStrategy
    gatekeeper_result_id: str

    def __post_init__(self) -> None:
        if self.interface_version != MERGE_INTERFACE_VERSION:
            raise DomainValidationError("unsupported merge interface_version")
        require_text(self.correlation_id, "correlation_id")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_enum(self.merge_strategy, MergeStrategy, "merge_strategy")
        require_text(self.gatekeeper_result_id, "gatekeeper_result_id")
        if self.repository_id <= 0 or self.pull_request_number <= 0:
            raise DomainValidationError(
                "repository and pull request identities must be positive"
            )
        if len(self.expected_head_sha) != 40:
            raise DomainValidationError("expected_head_sha must be a 40-character SHA")


@dataclass(frozen=True, slots=True)
class MergeResult:
    interface_version: str
    project_id: ProjectId
    milestone_id: MilestoneId
    pull_request_number: int
    status: MergeStatus
    merge_commit_sha: str | None = None
    merged_at: datetime | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.interface_version != MERGE_INTERFACE_VERSION:
            raise DomainValidationError("unsupported merge interface_version")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_enum(self.status, MergeStatus, "status")
        if self.pull_request_number <= 0:
            raise DomainValidationError("pull_request_number must be positive")
        if self.merged_at is not None:
            require_utc(self.merged_at, "merged_at")
        if self.merge_commit_sha is not None and len(self.merge_commit_sha) != 40:
            raise DomainValidationError("merge_commit_sha must be a 40-character SHA")
