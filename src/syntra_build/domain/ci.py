"""Provider-neutral contracts for CI observed at an exact pull-request head."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from syntra_build.domain.identifiers import MilestoneId, ProjectId

CI_INTERFACE_VERSION = "1.0"


class CIOverallStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class CICheckStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class CICheckConclusion(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class CIFailureClassification(StrEnum):
    IMPLEMENTATION = "IMPLEMENTATION"
    TEST = "TEST"
    CONFIGURATION = "CONFIGURATION"
    TRANSIENT_INFRASTRUCTURE = "TRANSIENT_INFRASTRUCTURE"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CICheck:
    name: str
    external_check_id: str
    status: CICheckStatus
    conclusion: CICheckConclusion | None = None
    started_at: str | None = None
    completed_at: str | None = None
    details_url: str | None = None
    failure_summary: str | None = None


@dataclass(frozen=True, slots=True)
class CIObservation:
    """One provider observation. An empty check tuple is never a passing set."""

    checks: tuple[CICheck, ...]
    external_workflow_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CIProgress:
    interface_version: str
    project_id: ProjectId
    milestone_id: MilestoneId
    pull_request_number: int
    head_sha: str
    overall_status: CIOverallStatus
    required_checks: tuple[CICheck, ...]


@dataclass(frozen=True, slots=True)
class CIResult:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    pull_request_number: int
    head_sha: str
    result: CIOverallStatus
    checks: tuple[CICheck, ...]
    failure_classification: CIFailureClassification | None
