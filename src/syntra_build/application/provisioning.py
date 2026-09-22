# ruff: noqa: E501
"""M18 deterministic, reconcile-before-retry repository provisioning."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from syntra_build.domain import (
    DesignPackageStatus,
    DocumentStatus,
    DocumentType,
    ProjectDocument,
    ProjectId,
    ProjectState,
    ProjectTransitionRequest,
    RepositoryVisibility,
)
from syntra_build.domain.provisioning import (
    GitHubRepository,
    RepositoryBaseline,
    RepositoryProvisioningStatus,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.design import (
    SQLiteProjectDocumentRepository,
)
from syntra_build.infrastructure.persistence.design_packages import (
    SQLiteDesignPackageRepository,
)
from syntra_build.infrastructure.persistence.projects import SQLiteProjectRepository
from syntra_build.infrastructure.persistence.provisioning import (
    SQLiteProvisioningRepository,
)


class ProvisioningFailure(StrEnum):
    PRECONDITION = "PRECONDITION"
    COLLISION = "COLLISION"
    AUTHENTICATION = "AUTHENTICATION"
    PROVIDER_REJECTION = "PROVIDER_REJECTION"
    TRANSIENT = "TRANSIENT"
    AMBIGUOUS = "AMBIGUOUS"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    VISIBILITY_MISMATCH = "VISIBILITY_MISMATCH"
    REMOTE_MISMATCH = "REMOTE_MISMATCH"
    DOCUMENT_MISMATCH = "DOCUMENT_MISMATCH"
    LOCAL_GIT = "LOCAL_GIT"
    PERSISTENCE = "PERSISTENCE"


class ProvisioningError(RuntimeError):
    def __init__(self, failure: ProvisioningFailure, message: str) -> None:
        self.failure = failure
        super().__init__(message)


class AmbiguousGitHubResult(RuntimeError):
    """A mutation may have succeeded and must be reconciled."""


class AmbiguousPushResult(RuntimeError):
    """A push may have succeeded and must be reconciled."""


@dataclass(frozen=True, slots=True)
class RemoteRepository:
    external_id: int
    owner: str
    name: str
    full_name: str
    visibility: RepositoryVisibility
    default_branch: str | None


class GitHubProvisioningGateway(Protocol):
    def get_repository(self, owner: str, name: str) -> RemoteRepository | None: ...
    def create_repository(
        self, name: str, visibility: RepositoryVisibility
    ) -> RemoteRepository: ...
    def configure_repository(self, owner: str, name: str) -> None: ...
    def main_sha(self, owner: str, name: str) -> str | None: ...
    def file_content(
        self, owner: str, name: str, sha: str, path: str
    ) -> bytes | None: ...


class InitialBaselineGit(Protocol):
    def create_commit(
        self, project_id: ProjectId, files: dict[str, bytes], message: str
    ) -> str: ...
    def push_main(
        self, project_id: ProjectId, remote_url: str, expected_sha: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    project_id: ProjectId
    project_name: str
    repository_full_name: str
    visibility: RepositoryVisibility
    external_repository_id: int
    baseline_commit_sha: str
    spec_hash: str
    agents_hash: str
    verified: bool
    project_state: ProjectState


class RepositoryProvisioningService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        github: GitHubProvisioningGateway,
        git: InitialBaselineGit,
        *,
        owner: str,
    ) -> None:
        self.connection = connection
        self.github, self.git, self.owner = github, git, owner

    def provision(
        self, project_id: ProjectId, occurred_at: datetime, correlation_id: str
    ) -> ProvisioningResult:
        projects = SQLiteProjectRepository(self.connection, lambda: str(uuid4()))
        packages = SQLiteDesignPackageRepository(self.connection)
        documents = SQLiteProjectDocumentRepository(self.connection)
        records = SQLiteProvisioningRepository(self.connection)
        project = projects.get(project_id)
        if project.state is not ProjectState.PROVISIONING or not project.canonical_name:
            raise ProvisioningError(
                ProvisioningFailure.PRECONDITION, "project is not provisionable"
            )
        row = self.connection.execute(
            """SELECT id FROM design_packages WHERE project_id=? AND status='APPROVED'
            ORDER BY approved_at DESC LIMIT 1""",
            (str(project_id),),
        ).fetchone()
        if row is None:
            raise ProvisioningError(
                ProvisioningFailure.PRECONDITION, "approved design package is required"
            )
        from syntra_build.domain import DesignPackageId

        package = packages.get(DesignPackageId.from_string(row["id"]))
        if (
            package.status is not DesignPackageStatus.APPROVED
            or package.repository_visibility is not project.repository_visibility
        ):
            raise ProvisioningError(
                ProvisioningFailure.PRECONDITION, "approved visibility is inconsistent"
            )
        spec, agents = (
            documents.get(project_id, package.spec_document_id),
            documents.get(project_id, package.agents_document_id),
        )
        self._validate_document(spec, DocumentType.SPEC)
        self._validate_document(agents, DocumentType.AGENTS)

        # This commit is the mandatory persist-before-create boundary.
        with transaction(self.connection):
            repository = records.ensure_intent(
                project_id,
                self.owner,
                project.canonical_name,
                package.repository_visibility,
                occurred_at,
            )
        live = self.github.get_repository(repository.owner, repository.repository_name)
        if live is not None and repository.external_repository_id is None:
            if repository.status is not RepositoryProvisioningStatus.CREATE_AMBIGUOUS:
                raise ProvisioningError(
                    ProvisioningFailure.COLLISION,
                    "repository name is already occupied without durable attribution",
                )
            self._validate_live(repository, live)
            with transaction(self.connection):
                records.identify(
                    repository.id, live.external_id, live.default_branch, occurred_at
                )
            refreshed = records.for_project(project_id)
            assert refreshed is not None
            repository = refreshed
        if live is None:
            if repository.external_repository_id is not None:
                raise ProvisioningError(
                    ProvisioningFailure.IDENTITY_MISMATCH,
                    "recorded repository is absent",
                )
            try:
                live = self.github.create_repository(
                    repository.repository_name, repository.visibility
                )
            except AmbiguousGitHubResult:
                with transaction(self.connection):
                    records.mark_ambiguous(repository.id, occurred_at)
                live = self.github.get_repository(
                    repository.owner, repository.repository_name
                )
                if live is None:
                    # Confirmed absence makes one bounded retry safe.
                    live = self.github.create_repository(
                        repository.repository_name, repository.visibility
                    )
            self._validate_live(repository, live)
            with transaction(self.connection):
                records.identify(
                    repository.id, live.external_id, live.default_branch, occurred_at
                )
            refreshed = records.for_project(project_id)
            assert refreshed is not None
            repository = refreshed
        self._validate_live(repository, live)
        baseline = records.baseline(project_id)
        if baseline is None:
            commit_sha = self.git.create_commit(
                project_id,
                {
                    "SPEC.md": spec.content.encode(),
                    "AGENTS.md": agents.content.encode(),
                },
                "Initial approved project design",
            )
            baseline = RepositoryBaseline(
                str(uuid4()),
                project_id,
                repository.id,
                commit_sha,
                spec.id,
                spec.revision,
                spec.content_hash,
                agents.id,
                agents.revision,
                agents.content_hash,
                occurred_at,
            )
            with transaction(self.connection):
                records.save_baseline(baseline)
        elif (
            baseline.github_repository_id != repository.id
            or baseline.spec_document_id != spec.id
            or baseline.spec_revision != spec.revision
            or baseline.spec_content_hash != spec.content_hash
            or baseline.agents_document_id != agents.id
            or baseline.agents_revision != agents.revision
            or baseline.agents_content_hash != agents.content_hash
        ):
            raise ProvisioningError(
                ProvisioningFailure.DOCUMENT_MISMATCH,
                "persisted baseline does not match the approved design",
            )
        remote_sha = self.github.main_sha(repository.owner, repository.repository_name)
        if remote_sha is None:
            try:
                self.git.push_main(
                    project_id,
                    f"https://github.com/{repository.full_name}.git",
                    baseline.commit_sha,
                )
            except AmbiguousPushResult:
                pass
            remote_sha = self.github.main_sha(
                repository.owner, repository.repository_name
            )
        if remote_sha != baseline.commit_sha:
            raise ProvisioningError(
                ProvisioningFailure.REMOTE_MISMATCH,
                "remote main does not match approved baseline",
            )
        self.github.configure_repository(repository.owner, repository.repository_name)
        live = self.github.get_repository(repository.owner, repository.repository_name)
        if live is None:
            raise ProvisioningError(
                ProvisioningFailure.IDENTITY_MISMATCH,
                "repository disappeared during verification",
            )
        self._validate_live(repository, live)
        if live.default_branch != "main":
            raise ProvisioningError(
                ProvisioningFailure.REMOTE_MISMATCH, "default branch is not main"
            )
        for path, expected in (
            ("SPEC.md", spec.content_hash),
            ("AGENTS.md", agents.content_hash),
        ):
            content = self.github.file_content(
                repository.owner, repository.repository_name, baseline.commit_sha, path
            )
            if content is None or sha256(content).hexdigest() != expected:
                raise ProvisioningError(
                    ProvisioningFailure.DOCUMENT_MISMATCH,
                    f"remote {path} differs from approval",
                )
        # Re-check the approval immediately before the atomic evidence/state transition.
        if packages.get(package.id).status is not DesignPackageStatus.APPROVED:
            raise ProvisioningError(
                ProvisioningFailure.PRECONDITION, "design approval is no longer valid"
            )
        with transaction(self.connection):
            records.verify(repository.id, baseline.id, occurred_at)
            project = projects.apply_transition(
                ProjectTransitionRequest(
                    project_id,
                    ProjectState.PROVISIONING,
                    ProjectState.READY,
                    "GitHub baseline independently verified",
                    "SYSTEM",
                    "repository-provisioner",
                    correlation_id,
                    occurred_at,
                )
            )
        return ProvisioningResult(
            project_id,
            project.name,
            repository.full_name,
            repository.visibility,
            live.external_id,
            baseline.commit_sha,
            spec.content_hash,
            agents.content_hash,
            True,
            project.state,
        )

    @staticmethod
    def _validate_document(
        document: ProjectDocument, expected_type: DocumentType
    ) -> None:
        if (
            document.document_type is not expected_type
            or document.status is not DocumentStatus.APPROVED
            or sha256(document.content.encode()).hexdigest() != document.content_hash
        ):
            raise ProvisioningError(
                ProvisioningFailure.DOCUMENT_MISMATCH, "approved document is invalid"
            )

    @staticmethod
    def _validate_live(repository: GitHubRepository, live: RemoteRepository) -> None:
        if (
            repository.external_repository_id is not None
            and live.external_id != repository.external_repository_id
        ):
            raise ProvisioningError(
                ProvisioningFailure.IDENTITY_MISMATCH, "immutable repository id differs"
            )
        if (live.owner, live.name, live.full_name) != (
            repository.owner,
            repository.repository_name,
            repository.full_name,
        ):
            raise ProvisioningError(
                ProvisioningFailure.IDENTITY_MISMATCH,
                "live repository identity differs",
            )
        if live.visibility is not repository.visibility:
            raise ProvisioningError(
                ProvisioningFailure.VISIBILITY_MISMATCH,
                "live repository visibility differs",
            )
