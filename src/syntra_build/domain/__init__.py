"""Provider-independent core domain records for Syntra Build."""

from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.events import WorkflowEvent
from syntra_build.domain.gates import GateState, GateType, HumanGate
from syntra_build.domain.identifiers import (
    GateId,
    JobId,
    MilestoneId,
    ProjectId,
    WorkflowEventId,
)
from syntra_build.domain.jobs import Job, JobState
from syntra_build.domain.milestones import Milestone, MilestoneState
from syntra_build.domain.projects import Project, ProjectState

__all__ = [
    "DomainValidationError",
    "GateId",
    "GateState",
    "GateType",
    "HumanGate",
    "Job",
    "JobId",
    "JobState",
    "Milestone",
    "MilestoneId",
    "MilestoneState",
    "Project",
    "ProjectId",
    "ProjectState",
    "WorkflowEvent",
    "WorkflowEventId",
]
