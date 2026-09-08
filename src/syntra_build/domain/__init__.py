"""Provider-independent core domain records for Syntra Build."""

from syntra_build.domain.errors import (
    DomainValidationError,
    InvalidBlockedRecoveryError,
    InvalidMilestoneTransitionError,
    InvalidProjectTransitionError,
)
from syntra_build.domain.events import WorkflowEvent
from syntra_build.domain.gates import GateState, GateType, HumanGate
from syntra_build.domain.identifiers import (
    GateId,
    JobId,
    MilestoneId,
    ProjectId,
    StateTransitionId,
    WorkflowEventId,
)
from syntra_build.domain.jobs import Job, JobState
from syntra_build.domain.milestone_state_machine import (
    ACTIVE_MILESTONE_STATES,
    VALID_MILESTONE_TRANSITIONS,
    MilestoneTransitionRequest,
    is_milestone_transition_allowed,
    validate_milestone_transition,
)
from syntra_build.domain.milestones import Milestone, MilestoneState
from syntra_build.domain.project_state_machine import (
    VALID_PROJECT_TRANSITIONS,
    ProjectTransitionRequest,
    is_transition_allowed,
    validate_transition,
)
from syntra_build.domain.projects import Project, ProjectState

__all__ = [
    "DomainValidationError",
    "InvalidProjectTransitionError",
    "InvalidBlockedRecoveryError",
    "InvalidMilestoneTransitionError",
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
    "MilestoneTransitionRequest",
    "ACTIVE_MILESTONE_STATES",
    "VALID_MILESTONE_TRANSITIONS",
    "Project",
    "ProjectId",
    "ProjectState",
    "ProjectTransitionRequest",
    "StateTransitionId",
    "VALID_PROJECT_TRANSITIONS",
    "WorkflowEvent",
    "WorkflowEventId",
    "is_transition_allowed",
    "is_milestone_transition_allowed",
    "validate_milestone_transition",
    "validate_transition",
]
