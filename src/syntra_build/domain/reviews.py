"""Strict provider-neutral contracts for exact-revision Architect review."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from syntra_build.domain._validation import require_text
from syntra_build.domain.design import ARCHITECT_INTERFACE_VERSION
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.milestones import MilestoneState


class ArchitectReviewVerdict(StrEnum):
    APPROVE = "APPROVE"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    HUMAN_TEST_REQUIRED = "HUMAN_TEST_REQUIRED"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
    BLOCKED = "BLOCKED"


class HumanDecisionKind(StrEnum):
    PRODUCT = "PRODUCT"
    TECHNICAL = "TECHNICAL"


class ReviewFindingSeverity(StrEnum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ArchitectHumanGateRequest:
    """Validated advisory detail for an M25 human intervention."""

    prompt: str
    resume_milestone_state: MilestoneState
    options: tuple[str, ...] = ()
    test_instructions: str | None = None
    artifact_reference: str | None = None
    decision_kind: HumanDecisionKind | None = None

    def __post_init__(self) -> None:
        require_text(self.prompt, "human_gate.prompt")
        if not isinstance(self.resume_milestone_state, MilestoneState):
            raise DomainValidationError("human gate requires a valid resume target")
        if len(set(self.options)) != len(self.options) or any(
            not isinstance(item, str) or not item.strip() for item in self.options
        ):
            raise DomainValidationError("human gate options must be unique text")
        if self.test_instructions is not None:
            require_text(self.test_instructions, "human_gate.test_instructions")
        if self.artifact_reference is not None:
            require_text(self.artifact_reference, "human_gate.artifact_reference")
        if self.decision_kind is not None and not isinstance(
            self.decision_kind, HumanDecisionKind
        ):
            raise DomainValidationError("invalid human decision kind")

    @classmethod
    def from_dict(cls, value: object) -> ArchitectHumanGateRequest:
        fields = {
            "prompt",
            "resume_milestone_state",
            "options",
            "test_instructions",
            "artifact_reference",
            "decision_kind",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or not isinstance(value["options"], list)
        ):
            raise DomainValidationError("malformed Architect human gate request")
        try:
            return cls(
                str(value["prompt"]),
                MilestoneState(str(value["resume_milestone_state"])),
                tuple(str(item) for item in value["options"]),
                str(value["test_instructions"])
                if value["test_instructions"] is not None
                else None,
                str(value["artifact_reference"])
                if value["artifact_reference"] is not None
                else None,
                HumanDecisionKind(str(value["decision_kind"]))
                if value["decision_kind"] is not None
                else None,
            )
        except (TypeError, ValueError) as error:
            raise DomainValidationError(
                "malformed Architect human gate request"
            ) from error

    def to_dict(self) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "resume_milestone_state": self.resume_milestone_state.value,
            "options": list(self.options),
            "test_instructions": self.test_instructions,
            "artifact_reference": self.artifact_reference,
            "decision_kind": self.decision_kind.value if self.decision_kind else None,
        }


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    finding_id: str
    severity: ReviewFindingSeverity
    requirement_ref: str
    description: str
    recommended_action: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.finding_id, "finding_id"),
            (self.requirement_ref, "requirement_ref"),
            (self.description, "description"),
            (self.recommended_action, "recommended_action"),
        ):
            require_text(value, name)
        if not isinstance(self.severity, ReviewFindingSeverity):
            raise DomainValidationError("severity must be a ReviewFindingSeverity")

    @classmethod
    def from_dict(cls, value: object) -> ReviewFinding:
        fields = {
            "finding_id",
            "severity",
            "requirement_ref",
            "description",
            "recommended_action",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise DomainValidationError("malformed Architect review finding")
        try:
            return cls(
                str(value["finding_id"]),
                ReviewFindingSeverity(str(value["severity"])),
                str(value["requirement_ref"]),
                str(value["description"]),
                str(value["recommended_action"]),
            )
        except DomainValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DomainValidationError("malformed Architect review finding") from error

    def to_dict(self) -> dict[str, str]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity.value,
            "requirement_ref": self.requirement_ref,
            "description": self.description,
            "recommended_action": self.recommended_action,
        }


@dataclass(frozen=True, slots=True)
class ArchitectReviewRequest:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    pull_request_number: int
    head_sha: str
    milestone_definition: dict[str, object]
    specification: dict[str, object]
    agents_instructions: dict[str, object]
    diff: str
    ci_result: dict[str, object]
    previous_findings: tuple[ReviewFinding, ...] = ()
    human_decisions: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.interface_version != ARCHITECT_INTERFACE_VERSION:
            raise DomainValidationError("unsupported Architect interface version")
        require_text(self.correlation_id, "correlation_id")
        if not isinstance(self.project_id, ProjectId) or not isinstance(
            self.milestone_id, MilestoneId
        ):
            raise DomainValidationError(
                "review requires stable project and milestone IDs"
            )
        if type(self.pull_request_number) is not int or self.pull_request_number < 1:
            raise DomainValidationError("pull_request_number must be positive")
        if not isinstance(self.head_sha, str) or len(self.head_sha) != 40:
            raise DomainValidationError("head_sha must be a 40-character SHA")
        require_text(self.diff, "diff")
        for value, name in (
            (self.milestone_definition, "milestone_definition"),
            (self.specification, "specification"),
            (self.agents_instructions, "agents_instructions"),
            (self.ci_result, "ci_result"),
        ):
            if not isinstance(value, dict) or not value:
                raise DomainValidationError(f"{name} must be a non-empty object")
        if any(not isinstance(item, ReviewFinding) for item in self.previous_findings):
            raise DomainValidationError("previous_findings must be typed findings")
        if any(not isinstance(item, dict) for item in self.human_decisions):
            raise DomainValidationError("human_decisions must contain objects")

    def to_dict(self) -> dict[str, object]:
        return {
            "interface_version": self.interface_version,
            "correlation_id": self.correlation_id,
            "project_id": str(self.project_id),
            "milestone_id": str(self.milestone_id),
            "pull_request_number": self.pull_request_number,
            "head_sha": self.head_sha,
            "milestone_definition": self.milestone_definition,
            "specification": self.specification,
            "agents_instructions": self.agents_instructions,
            "diff": self.diff,
            "ci_result": self.ci_result,
            "previous_findings": [item.to_dict() for item in self.previous_findings],
            "human_decisions": list(self.human_decisions),
        }


@dataclass(frozen=True, slots=True)
class ArchitectReview:
    interface_version: str
    correlation_id: str
    project_id: ProjectId
    milestone_id: MilestoneId
    pull_request_number: int
    reviewed_sha: str
    verdict: ArchitectReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...]
    human_gate: ArchitectHumanGateRequest | None = None

    def __post_init__(self) -> None:
        if self.interface_version != ARCHITECT_INTERFACE_VERSION:
            raise DomainValidationError("unsupported Architect interface version")
        require_text(self.correlation_id, "correlation_id")
        require_text(self.summary, "summary")
        if not isinstance(self.project_id, ProjectId) or not isinstance(
            self.milestone_id, MilestoneId
        ):
            raise DomainValidationError(
                "review requires stable project and milestone IDs"
            )
        if type(self.pull_request_number) is not int or self.pull_request_number < 1:
            raise DomainValidationError("pull_request_number must be positive")
        if not isinstance(self.reviewed_sha, str) or len(self.reviewed_sha) != 40:
            raise DomainValidationError("reviewed_sha must be a 40-character SHA")
        if not isinstance(self.verdict, ArchitectReviewVerdict):
            raise DomainValidationError("invalid Architect review verdict")
        if any(not isinstance(item, ReviewFinding) for item in self.findings):
            raise DomainValidationError("findings must be typed findings")
        if (
            self.verdict is ArchitectReviewVerdict.CHANGES_REQUIRED
            and not self.findings
        ):
            raise DomainValidationError("CHANGES_REQUIRED requires actionable findings")
        if self.verdict is ArchitectReviewVerdict.APPROVE and self.findings:
            raise DomainValidationError("APPROVE cannot contain unresolved findings")
        needs_gate = self.verdict in {
            ArchitectReviewVerdict.HUMAN_TEST_REQUIRED,
            ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED,
        }
        if needs_gate != (self.human_gate is not None):
            raise DomainValidationError(
                "human verdicts require exactly one gate request"
            )
        if self.human_gate is not None:
            if self.verdict is ArchitectReviewVerdict.HUMAN_DECISION_REQUIRED:
                if not self.human_gate.options:
                    raise DomainValidationError(
                        "human decisions require allowed options"
                    )
                if self.human_gate.decision_kind is None:
                    raise DomainValidationError(
                        "human decisions require a decision kind"
                    )
                if (
                    self.human_gate.resume_milestone_state
                    is not MilestoneState.ARCHITECT_REVIEW
                ):
                    raise DomainValidationError(
                        "human decisions require ARCHITECT_REVIEW resume"
                    )
            elif (
                self.human_gate.options
                or self.human_gate.test_instructions is None
                or self.human_gate.decision_kind is not None
                or self.human_gate.resume_milestone_state
                is not MilestoneState.ARCHITECT_REVIEW
            ):
                raise DomainValidationError(
                    "human tests require instructions and ARCHITECT_REVIEW resume"
                )

    @classmethod
    def from_dict(cls, value: object) -> ArchitectReview:
        required = {
            "interface_version",
            "correlation_id",
            "project_id",
            "milestone_id",
            "pull_request_number",
            "reviewed_sha",
            "verdict",
            "summary",
            "findings",
        }
        allowed = required | {"human_gate"}
        if (
            not isinstance(value, dict)
            or not required.issubset(value)
            or not set(value).issubset(allowed)
            or not isinstance(value["findings"], list)
        ):
            raise DomainValidationError("malformed Architect review")
        try:
            number = value["pull_request_number"]
            if type(number) is not int:
                raise TypeError
            return cls(
                str(value["interface_version"]),
                str(value["correlation_id"]),
                ProjectId.from_string(str(value["project_id"])),
                MilestoneId.from_string(str(value["milestone_id"])),
                number,
                str(value["reviewed_sha"]),
                ArchitectReviewVerdict(str(value["verdict"])),
                str(value["summary"]),
                tuple(ReviewFinding.from_dict(item) for item in value["findings"]),
                ArchitectHumanGateRequest.from_dict(value["human_gate"])
                if value.get("human_gate") is not None
                else None,
            )
        except DomainValidationError:
            raise
        except (TypeError, ValueError) as error:
            raise DomainValidationError("malformed Architect review") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "interface_version": self.interface_version,
            "correlation_id": self.correlation_id,
            "project_id": str(self.project_id),
            "milestone_id": str(self.milestone_id),
            "pull_request_number": self.pull_request_number,
            "reviewed_sha": self.reviewed_sha,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "findings": [item.to_dict() for item in self.findings],
            "human_gate": self.human_gate.to_dict() if self.human_gate else None,
        }


@dataclass(frozen=True, slots=True)
class ArchitectReworkTask:
    task_type: str
    project_id: ProjectId
    milestone_id: MilestoneId
    pull_request_number: int
    branch: str
    reviewed_sha: str
    findings: tuple[ReviewFinding, ...]
    agents_instructions: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_type": self.task_type,
            "project_id": str(self.project_id),
            "milestone_id": str(self.milestone_id),
            "pull_request_number": self.pull_request_number,
            "branch": self.branch,
            "reviewed_sha": self.reviewed_sha,
            "findings": [item.to_dict() for item in self.findings],
            "agents_instructions": self.agents_instructions,
            "constraints": [
                "Do not push, create or merge pull requests",
                "Do not use privileged GitHub credentials",
            ],
        }
