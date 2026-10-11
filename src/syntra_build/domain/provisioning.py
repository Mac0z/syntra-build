"""Provider-neutral records for the trusted initial repository baseline."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from re import fullmatch

from syntra_build.domain._validation import (
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import ProjectDocumentId, ProjectId
from syntra_build.domain.projects import RepositoryVisibility


class RepositoryProvisioningStatus(StrEnum):
    INTENDED = "INTENDED"
    CREATE_AMBIGUOUS = "CREATE_AMBIGUOUS"
    IDENTIFIED = "IDENTIFIED"
    BASELINE_PUSHED = "BASELINE_PUSHED"
    VERIFIED = "VERIFIED"


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    id: str
    project_id: ProjectId
    provider: str
    owner: str
    repository_name: str
    full_name: str
    external_repository_id: int | None
    visibility: RepositoryVisibility
    default_branch: str | None
    status: RepositoryProvisioningStatus
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RepositoryBaseline:
    id: str
    project_id: ProjectId
    github_repository_id: str
    commit_sha: str
    spec_document_id: ProjectDocumentId
    spec_revision: int
    spec_content_hash: str
    agents_document_id: ProjectDocumentId
    agents_revision: int
    agents_content_hash: str
    created_at: datetime
    verified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RepositoryCIIntent:
    """Immutable approved inputs and exact trusted CI bytes before side effects."""

    project_id: ProjectId
    github_repository_id: str
    spec_document_id: ProjectDocumentId
    spec_revision: int
    spec_content_hash: str
    agents_document_id: ProjectDocumentId
    agents_revision: int
    agents_content_hash: str
    profile_id: str
    python_version: str
    workflow_path: str
    workflow_content: str
    workflow_content_hash: str
    default_branch: str
    created_at: datetime

    def __post_init__(self) -> None:
        require_identifier(self.project_id, ProjectId, "project_id")
        for name in ("spec_document_id", "agents_document_id"):
            require_identifier(getattr(self, name), ProjectDocumentId, name)
        for name in ("spec_revision", "agents_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise DomainValidationError(f"{name} must be a positive integer")
        for name in (
            "github_repository_id",
            "profile_id",
            "python_version",
            "workflow_content",
        ):
            require_text(getattr(self, name), name)
        for name in (
            "spec_content_hash",
            "agents_content_hash",
            "workflow_content_hash",
        ):
            if fullmatch(r"[0-9a-f]{64}", getattr(self, name)) is None:
                raise DomainValidationError(f"{name} must be a SHA-256 hash")
        if (
            sha256(self.workflow_content.encode()).hexdigest()
            != self.workflow_content_hash
        ):
            raise DomainValidationError("workflow content does not match its hash")
        if self.workflow_path != ".github/workflows/ci.yml":
            raise DomainValidationError("trusted CI workflow path is invalid")
        if self.default_branch != "main":
            raise DomainValidationError("trusted CI default branch must be main")
        require_utc(self.created_at, "created_at")
