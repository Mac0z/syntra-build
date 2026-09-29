"""Strict provider-neutral milestone task contracts introduced by M32."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from syntra_build.domain._validation import require_identifier, require_text
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import JobId, MilestoneId, ProjectId

ARCHITECT_TASK_INTERFACE_VERSION = "1.0"


class ArchitectTaskType(StrEnum):
    IMPLEMENT = "IMPLEMENT"
    CI_REWORK = "CI_REWORK"
    REVIEW_REWORK = "REVIEW_REWORK"
    HUMAN_TEST_REWORK = "HUMAN_TEST_REWORK"


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise DomainValidationError(f"{name} must contain non-empty strings")
    return tuple(value)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DomainValidationError(f"{name} must be an object")
    return dict(value)


@dataclass(frozen=True, slots=True)
class ArchitectTaskRequest:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    job_id: JobId
    task_type: ArchitectTaskType
    spec_revision: str
    spec_content: str
    agents_revision: str
    agents_content: str
    milestone_definition: Mapping[str, object]
    repository_context: Mapping[str, object]
    previous_milestone_summaries: tuple[str, ...] = ()
    failure_evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.interface_version != ARCHITECT_TASK_INTERFACE_VERSION:
            raise DomainValidationError("unsupported Architect task interface version")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        require_identifier(self.job_id, JobId, "job_id")
        if not isinstance(self.task_type, ArchitectTaskType):
            raise DomainValidationError("task_type must be an ArchitectTaskType")
        for value, name in (
            (self.correlation_id, "correlation_id"),
            (self.spec_revision, "spec_revision"),
            (self.spec_content, "spec_content"),
            (self.agents_revision, "agents_revision"),
            (self.agents_content, "agents_content"),
        ):
            require_text(value, name)
        if not isinstance(self.milestone_definition, Mapping):
            raise DomainValidationError("milestone_definition must be an object")
        if not isinstance(self.repository_context, Mapping):
            raise DomainValidationError("repository_context must be an object")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.previous_milestone_summaries
        ):
            raise DomainValidationError(
                "previous milestone summaries must be non-empty"
            )
        if self.failure_evidence is not None and not isinstance(
            self.failure_evidence, Mapping
        ):
            raise DomainValidationError("failure_evidence must be an object")

    def to_dict(self) -> dict[str, object]:
        return {
            "interface_version": self.interface_version,
            "correlation_id": self.correlation_id,
            "project_id": str(self.project_id),
            "milestone_id": str(self.milestone_id),
            "job_id": str(self.job_id),
            "task_type": self.task_type.value,
            "spec_revision": self.spec_revision,
            "spec_content": self.spec_content,
            "agents_revision": self.agents_revision,
            "agents_content": self.agents_content,
            "milestone_definition": dict(self.milestone_definition),
            "repository_context": dict(self.repository_context),
            "previous_milestone_summaries": list(self.previous_milestone_summaries),
            "failure_evidence": dict(self.failure_evidence or {}),
        }


@dataclass(frozen=True, slots=True)
class ArchitectTask:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    task_type: ArchitectTaskType
    objective: str
    requirements: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    tests_required: tuple[str, ...]
    explicit_exclusions: tuple[str, ...]
    files_of_interest: tuple[str, ...] = ()
    architecture_notes: tuple[str, ...] = ()
    prior_failure_summary: str | None = None
    prior_review_findings: tuple[str, ...] = ()
    human_feedback: str | None = None
    recommended_commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.interface_version != ARCHITECT_TASK_INTERFACE_VERSION:
            raise DomainValidationError("unsupported Architect task interface version")
        require_identifier(self.project_id, ProjectId, "project_id")
        require_identifier(self.milestone_id, MilestoneId, "milestone_id")
        if not isinstance(self.task_type, ArchitectTaskType):
            raise DomainValidationError("task_type must be an ArchitectTaskType")
        require_text(self.correlation_id, "correlation_id")
        require_text(self.objective, "objective")
        for name in (
            "requirements",
            "acceptance_criteria",
            "constraints",
            "tests_required",
            "explicit_exclusions",
            "files_of_interest",
            "architecture_notes",
            "prior_review_findings",
            "recommended_commands",
        ):
            if any(
                not isinstance(item, str) or not item.strip()
                for item in getattr(self, name)
            ):
                raise DomainValidationError(f"{name} must contain non-empty strings")
        for value, name in (
            (self.prior_failure_summary, "prior_failure_summary"),
            (self.human_feedback, "human_feedback"),
        ):
            if value is not None:
                require_text(value, name)

    @classmethod
    def from_dict(cls, value: object) -> ArchitectTask:
        required = {
            "interface_version",
            "correlation_id",
            "project_id",
            "milestone_id",
            "task_type",
            "objective",
            "requirements",
            "acceptance_criteria",
            "constraints",
            "tests_required",
            "explicit_exclusions",
        }
        optional = {
            "files_of_interest",
            "architecture_notes",
            "prior_failure_summary",
            "prior_review_findings",
            "human_feedback",
            "recommended_commands",
        }
        if (
            not isinstance(value, dict)
            or not required <= set(value)
            or set(value) - required - optional
        ):
            raise DomainValidationError("malformed Architect task")
        try:
            return cls(
                str(value["interface_version"]),
                str(value["correlation_id"]),
                ProjectId.from_string(value["project_id"]),
                MilestoneId.from_string(value["milestone_id"]),
                ArchitectTaskType(value["task_type"]),
                str(value["objective"]),
                requirements=_strings(value["requirements"], "requirements"),
                acceptance_criteria=_strings(
                    value["acceptance_criteria"], "acceptance_criteria"
                ),
                constraints=_strings(value["constraints"], "constraints"),
                tests_required=_strings(value["tests_required"], "tests_required"),
                explicit_exclusions=_strings(
                    value["explicit_exclusions"], "explicit_exclusions"
                ),
                files_of_interest=_strings(
                    value.get("files_of_interest", []), "files_of_interest"
                ),
                architecture_notes=_strings(
                    value.get("architecture_notes", []), "architecture_notes"
                ),
                prior_failure_summary=value.get("prior_failure_summary"),
                prior_review_findings=_strings(
                    value.get("prior_review_findings", []), "prior_review_findings"
                ),
                human_feedback=value.get("human_feedback"),
                recommended_commands=_strings(
                    value.get("recommended_commands", []), "recommended_commands"
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainValidationError("malformed Architect task") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "interface_version": self.interface_version,
            "correlation_id": self.correlation_id,
            "project_id": str(self.project_id),
            "milestone_id": str(self.milestone_id),
            "task_type": self.task_type.value,
            "objective": self.objective,
            "requirements": list(self.requirements),
            "acceptance_criteria": list(self.acceptance_criteria),
            "constraints": list(self.constraints),
            "tests_required": list(self.tests_required),
            "explicit_exclusions": list(self.explicit_exclusions),
            "files_of_interest": list(self.files_of_interest),
            "architecture_notes": list(self.architecture_notes),
            "prior_failure_summary": self.prior_failure_summary,
            "prior_review_findings": list(self.prior_review_findings),
            "human_feedback": self.human_feedback,
            "recommended_commands": list(self.recommended_commands),
        }
