"""Provider-neutral contracts for milestone implementation pull requests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from syntra_build.domain.identifiers import MilestoneId, ProjectId

PULL_REQUEST_INTERFACE_VERSION = "1.0"


class PullRequestState(StrEnum):
    OPEN = "OPEN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class PullRequestCreateRequest:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    repository_id: int
    head_branch: str
    base_branch: str
    head_sha: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class PullRequestDescriptor:
    interface_version: str
    project_id: ProjectId
    milestone_id: MilestoneId
    repository_id: int
    pull_request_number: int
    state: PullRequestState
    head_branch: str
    base_branch: str
    head_sha: str
    web_url: str
    merged_at: str | None = None
    merge_commit_sha: str | None = None
    closed_at: str | None = None
