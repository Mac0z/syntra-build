"""Immutable workflow-event value contract."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from syntra_build.domain._validation import (
    require_identifier,
    require_text,
    require_utc,
)
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import (
    GateId,
    JobId,
    MilestoneId,
    ProjectId,
    WorkflowEventId,
)


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    id: WorkflowEventId
    project_id: ProjectId
    event_type: str
    occurred_at: datetime
    correlation_id: str
    milestone_id: MilestoneId | None = None
    job_id: JobId | None = None
    gate_id: GateId | None = None
    payload: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        require_identifier(self.id, WorkflowEventId, "id")
        require_identifier(self.project_id, ProjectId, "project_id")
        if self.milestone_id is not None:
            require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        if self.job_id is not None:
            require_identifier(self.job_id, JobId, "job_id")
        if self.gate_id is not None:
            require_identifier(self.gate_id, GateId, "gate_id")
        require_text(self.event_type, "event_type")
        require_text(self.correlation_id, "correlation_id")
        require_utc(self.occurred_at, "occurred_at")
        if self.payload is None:
            object.__setattr__(self, "payload", MappingProxyType({}))
        elif not isinstance(self.payload, Mapping):
            raise DomainValidationError("payload must be a mapping")
        else:
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
