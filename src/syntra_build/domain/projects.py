"""Project domain records and approved lifecycle vocabulary."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from syntra_build.domain._validation import (
    require_enum,
    require_identifier,
    require_text,
    require_timestamp_order,
    require_utc,
)
from syntra_build.domain.identifiers import ProjectId


class ProjectState(StrEnum):
    NEW = "NEW"
    DESIGNING = "DESIGNING"
    DESIGN_APPROVAL = "DESIGN_APPROVAL"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    BUILDING = "BUILDING"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    COMPLETING = "COMPLETING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class Project:
    id: ProjectId
    name: str
    state: ProjectState
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        require_identifier(self.id, ProjectId, "id")
        require_text(self.name, "name")
        require_enum(self.state, ProjectState, "state")
        require_utc(self.created_at, "created_at")
        require_utc(self.updated_at, "updated_at")
        require_timestamp_order(
            self.created_at, self.updated_at, "created_at", "updated_at"
        )
