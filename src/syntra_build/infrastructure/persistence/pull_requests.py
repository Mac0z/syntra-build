# ruff: noqa: E501
"""SQLite records for durable PR intent and verified PR state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.pull_requests import (
    PullRequestCreateRequest,
    PullRequestDescriptor,
    PullRequestState,
)


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    github_repository_id: str
    external_pr_number: int
    state: PullRequestState
    head_branch: str
    base_branch: str
    head_sha: str
    web_url: str
    title: str


class SQLitePullRequestRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_intent(
        self,
        intent_id: str,
        github_repository_id: str,
        request: PullRequestCreateRequest,
        body_hash: str,
        now: datetime,
    ) -> None:
        self.connection.execute(
            """INSERT INTO pull_request_creation_intents
            (id,project_id,milestone_id,github_repository_id,correlation_id,
             head_branch,base_branch,head_sha,title,body_hash,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?, 'RECONCILING',?,?)
            ON CONFLICT(milestone_id) DO UPDATE SET
              head_sha=excluded.head_sha,title=excluded.title,body_hash=excluded.body_hash,
              correlation_id=excluded.correlation_id,status='RECONCILING',updated_at=excluded.updated_at""",
            (
                intent_id,
                str(request.project_id),
                str(request.milestone_id),
                github_repository_id,
                request.correlation_id,
                request.head_branch,
                request.base_branch,
                request.head_sha,
                request.title,
                body_hash,
                now.isoformat(),
                now.isoformat(),
            ),
        )

    def for_milestone(self, milestone_id: MilestoneId) -> PullRequestRecord | None:
        row = self.connection.execute(
            "SELECT * FROM pull_requests WHERE milestone_id=? ORDER BY created_at LIMIT 1",
            (str(milestone_id),),
        ).fetchone()
        if row is None:
            return None
        return PullRequestRecord(
            row["id"],
            ProjectId.from_string(row["project_id"]),
            MilestoneId.from_string(row["milestone_id"]),
            row["github_repository_id"],
            row["external_pr_number"],
            PullRequestState(row["state"]),
            row["head_branch"],
            row["base_branch"],
            row["head_sha"],
            row["web_url"],
            row["title"],
        )

    def save_verified(
        self,
        internal_id: str,
        github_repository_id: str,
        descriptor: PullRequestDescriptor,
        title: str,
        now: datetime,
    ) -> PullRequestRecord:
        existing = self.for_milestone(descriptor.milestone_id)
        if existing is None:
            self.connection.execute(
                """INSERT INTO pull_requests
                (id,project_id,milestone_id,github_repository_id,external_pr_number,state,
                 head_branch,base_branch,head_sha,web_url,title,created_at,updated_at,
                 merged_at,merge_commit_sha,closed_at,last_reconciled_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    internal_id,
                    str(descriptor.project_id),
                    str(descriptor.milestone_id),
                    github_repository_id,
                    descriptor.pull_request_number,
                    descriptor.state,
                    descriptor.head_branch,
                    descriptor.base_branch,
                    descriptor.head_sha,
                    descriptor.web_url,
                    title,
                    now.isoformat(),
                    now.isoformat(),
                    descriptor.merged_at,
                    descriptor.merge_commit_sha,
                    descriptor.closed_at,
                    now.isoformat(),
                ),
            )
            pr_id = internal_id
        else:
            if (
                existing.project_id != descriptor.project_id
                or existing.github_repository_id != github_repository_id
                or existing.external_pr_number != descriptor.pull_request_number
                or existing.head_branch != descriptor.head_branch
                or existing.base_branch != descriptor.base_branch
            ):
                raise ValueError("persisted pull request identity differs")
            self.connection.execute(
                """UPDATE pull_requests SET state=?,head_sha=?,web_url=?,title=?,updated_at=?,
                merged_at=?,merge_commit_sha=?,closed_at=?,last_reconciled_at=? WHERE id=?""",
                (
                    descriptor.state,
                    descriptor.head_sha,
                    descriptor.web_url,
                    title,
                    now.isoformat(),
                    descriptor.merged_at,
                    descriptor.merge_commit_sha,
                    descriptor.closed_at,
                    now.isoformat(),
                    existing.id,
                ),
            )
            pr_id = existing.id
        self.connection.execute(
            "UPDATE milestones SET active_pull_request_id=? WHERE id=?",
            (pr_id, str(descriptor.milestone_id)),
        )
        self.connection.execute(
            "UPDATE pull_request_creation_intents SET status='VERIFIED',updated_at=? WHERE milestone_id=?",
            (now.isoformat(), str(descriptor.milestone_id)),
        )
        result = self.for_milestone(descriptor.milestone_id)
        assert result is not None
        return result
