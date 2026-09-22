# ruff: noqa: E501
"""SQLite repository identity and immutable initial-baseline evidence."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from syntra_build.domain import ProjectDocumentId, ProjectId, RepositoryVisibility
from syntra_build.domain.provisioning import (
    GitHubRepository,
    RepositoryBaseline,
    RepositoryProvisioningStatus,
)
from syntra_build.infrastructure.persistence.errors import PersistenceError


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _required_time(value: str) -> datetime:
    parsed = _time(value)
    assert parsed is not None
    return parsed


class SQLiteProvisioningRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_intent(
        self,
        project_id: ProjectId,
        owner: str,
        name: str,
        visibility: RepositoryVisibility,
        occurred_at: datetime,
    ) -> GitHubRepository:
        existing = self.for_project(project_id)
        full_name = f"{owner}/{name}"
        if existing is not None:
            if (
                existing.owner,
                existing.repository_name,
                existing.full_name,
                existing.visibility,
            ) != (owner, name, full_name, visibility):
                raise PersistenceError("persisted repository intent does not match")
            return existing
        try:
            self.connection.execute(
                """INSERT INTO github_repositories
                (id,project_id,provider,owner,repository_name,full_name,visibility,status,
                 created_at,updated_at) VALUES (?,?,?,?,?,?,?,'INTENDED',?,?)""",
                (
                    str(uuid4()),
                    str(project_id),
                    "github",
                    owner,
                    name,
                    full_name,
                    visibility.value,
                    occurred_at.isoformat(),
                    occurred_at.isoformat(),
                ),
            )
        except sqlite3.Error as error:
            raise PersistenceError(
                "repository intent could not be persisted"
            ) from error
        result = self.for_project(project_id)
        assert result is not None
        return result

    def for_project(self, project_id: ProjectId) -> GitHubRepository | None:
        row = self.connection.execute(
            "SELECT * FROM github_repositories WHERE project_id=?", (str(project_id),)
        ).fetchone()
        if row is None:
            return None
        return GitHubRepository(
            row["id"],
            project_id,
            row["provider"],
            row["owner"],
            row["repository_name"],
            row["full_name"],
            row["external_repository_id"],
            RepositoryVisibility(row["visibility"]),
            row["default_branch"],
            RepositoryProvisioningStatus(row["status"]),
            _required_time(row["created_at"]),
            _required_time(row["updated_at"]),
            _time(row["verified_at"]),
        )

    def mark_ambiguous(self, repository_id: str, occurred_at: datetime) -> None:
        self._update(repository_id, "CREATE_AMBIGUOUS", occurred_at)

    def identify(
        self,
        repository_id: str,
        external_id: int,
        default_branch: str | None,
        occurred_at: datetime,
    ) -> None:
        try:
            changed = self.connection.execute(
                """UPDATE github_repositories SET external_repository_id=?,default_branch=?,
                status='IDENTIFIED',updated_at=? WHERE id=? AND
                (external_repository_id IS NULL OR external_repository_id=?)""",
                (
                    external_id,
                    default_branch,
                    occurred_at.isoformat(),
                    repository_id,
                    external_id,
                ),
            ).rowcount
        except sqlite3.Error as error:
            raise PersistenceError(
                "repository identity could not be recorded"
            ) from error
        if changed != 1:
            raise PersistenceError("external repository identity mismatch")

    def _update(self, repository_id: str, status: str, occurred_at: datetime) -> None:
        try:
            self.connection.execute(
                "UPDATE github_repositories SET status=?,updated_at=? WHERE id=?",
                (status, occurred_at.isoformat(), repository_id),
            )
        except sqlite3.Error as error:
            raise PersistenceError("repository status could not be recorded") from error

    def save_baseline(self, baseline: RepositoryBaseline) -> None:
        try:
            self.connection.execute(
                """INSERT INTO repository_baselines VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(project_id) DO NOTHING""",
                (
                    baseline.id,
                    str(baseline.project_id),
                    baseline.github_repository_id,
                    baseline.commit_sha,
                    str(baseline.spec_document_id),
                    baseline.spec_revision,
                    baseline.spec_content_hash,
                    str(baseline.agents_document_id),
                    baseline.agents_revision,
                    baseline.agents_content_hash,
                    baseline.created_at.isoformat(),
                ),
            )
        except sqlite3.Error as error:
            raise PersistenceError(
                "repository baseline could not be persisted"
            ) from error

    def baseline(self, project_id: ProjectId) -> RepositoryBaseline | None:
        row = self.connection.execute(
            "SELECT * FROM repository_baselines WHERE project_id=?", (str(project_id),)
        ).fetchone()
        if row is None:
            return None
        return RepositoryBaseline(
            row["id"],
            project_id,
            row["github_repository_id"],
            row["commit_sha"],
            ProjectDocumentId.from_string(row["spec_document_id"]),
            row["spec_revision"],
            row["spec_content_hash"],
            ProjectDocumentId.from_string(row["agents_document_id"]),
            row["agents_revision"],
            row["agents_content_hash"],
            _required_time(row["created_at"]),
            _time(row["verified_at"]),
        )

    def verify(
        self, repository_id: str, baseline_id: str, occurred_at: datetime
    ) -> None:
        try:
            self.connection.execute(
                "UPDATE repository_baselines SET verified_at=? WHERE id=? AND verified_at IS NULL",
                (occurred_at.isoformat(), baseline_id),
            )
            self.connection.execute(
                """UPDATE github_repositories SET status='VERIFIED',verified_at=?,updated_at=?
                WHERE id=?""",
                (occurred_at.isoformat(), occurred_at.isoformat(), repository_id),
            )
        except sqlite3.Error as error:
            raise PersistenceError(
                "verification evidence could not be persisted"
            ) from error
