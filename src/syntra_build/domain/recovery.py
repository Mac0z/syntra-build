"""Provider-neutral recovery records and lifecycle."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId


class RecoveryLifecycle(StrEnum):
    STARTING = "STARTING"
    RECOVERING = "RECOVERING"
    READY = "READY"


class RecoveryDisposition(StrEnum):
    RECONCILED = "RECONCILED"
    SAFE_RETRY = "SAFE_RETRY"
    RESTORED_WAIT = "RESTORED_WAIT"
    ABANDONED = "ABANDONED"
    BLOCKED = "BLOCKED"
    NO_ACTION = "NO_ACTION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RecoverySubject:
    project_id: ProjectId
    project_state: str
    milestone_id: MilestoneId | None = None
    milestone_state: str | None = None
    job_id: JobId | None = None
    job_state: str | None = None

    @property
    def persisted_state(self) -> str:
        return self.job_state or self.milestone_state or self.project_state


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    category: str
    disposition: RecoveryDisposition
    action: str
    observed: Mapping[str, object] = field(default_factory=dict)
    reason: str | None = None
