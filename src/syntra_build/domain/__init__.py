"""Provider-independent core domain records for Syntra Build."""

from syntra_build.domain.errors import (
    DomainValidationError,
    InvalidBlockedRecoveryError,
    InvalidJobTransitionError,
    InvalidMilestoneTransitionError,
    InvalidProjectTransitionError,
    InvalidRetryMetadataError,
)
from syntra_build.domain.events import WorkflowEvent
from syntra_build.domain.gate_state_machine import (
    VALID_GATE_TRANSITIONS,
    GateTransitionRequest,
    is_gate_transition_allowed,
    validate_gate_transition,
)
from syntra_build.domain.gates import (
    DesignApprovalResponse,
    ExpectedResponseType,
    GateState,
    GateType,
    HumanGate,
    HumanGateResponse,
    HumanTestResponse,
    validate_response,
)
from syntra_build.domain.identifiers import (
    GateId,
    JobId,
    MilestoneId,
    ProjectId,
    StateTransitionId,
    WorkflowEventId,
)
from syntra_build.domain.job_state_machine import (
    TERMINAL_JOB_STATES,
    VALID_JOB_TRANSITIONS,
    JobTransitionRequest,
    validate_job_transition,
)
from syntra_build.domain.jobs import (
    Job,
    JobAttempt,
    JobAttemptState,
    JobState,
    WorkerClass,
    consumes_worker_capacity,
)
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
    "HumanGateResponse",
    "ExpectedResponseType",
    "DesignApprovalResponse",
    "HumanTestResponse",
    "GateTransitionRequest",
    "VALID_GATE_TRANSITIONS",
    "is_gate_transition_allowed",
    "validate_gate_transition",
    "validate_response",
    "Job",
    "JobId",
    "JobState",
    "JobAttempt",
    "JobAttemptState",
    "WorkerClass",
    "JobTransitionRequest",
    "VALID_JOB_TRANSITIONS",
    "TERMINAL_JOB_STATES",
    "validate_job_transition",
    "consumes_worker_capacity",
    "InvalidJobTransitionError",
    "InvalidRetryMetadataError",
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
