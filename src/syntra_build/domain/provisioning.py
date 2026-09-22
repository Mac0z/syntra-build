"""Provider-neutral records for the trusted initial repository baseline."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

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
