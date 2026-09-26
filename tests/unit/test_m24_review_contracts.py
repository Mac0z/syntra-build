from __future__ import annotations

from typing import Any, cast

import pytest

from syntra_build.adapters.architect.openai import REVIEW_SCHEMA
from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.reviews import (
    ArchitectReview,
    ArchitectReviewVerdict,
    ReviewFinding,
    ReviewFindingSeverity,
)


def _payload() -> dict[str, object]:
    return {
        "interface_version": "1.0",
        "correlation_id": "review-1",
        "project_id": str(ProjectId.generate()),
        "milestone_id": str(MilestoneId.generate()),
        "pull_request_number": 31,
        "reviewed_sha": "a" * 40,
        "verdict": "CHANGES_REQUIRED",
        "summary": "One acceptance criterion needs work.",
        "findings": [
            {
                "finding_id": "M24-1",
                "severity": "major",
                "requirement_ref": "M24 acceptance 2",
                "description": "The current-head guard is absent.",
                "recommended_action": "Re-read and compare the live PR head.",
            }
        ],
    }


def test_review_contract_strictly_parses_typed_findings() -> None:
    review = ArchitectReview.from_dict(_payload())
    assert review.verdict is ArchitectReviewVerdict.CHANGES_REQUIRED
    assert review.findings == (
        ReviewFinding(
            "M24-1",
            ReviewFindingSeverity.MAJOR,
            "M24 acceptance 2",
            "The current-head guard is absent.",
            "Re-read and compare the live PR head.",
        ),
    )


@pytest.mark.parametrize("verdict", ["ACCEPT", "MERGE", "approved"])
def test_review_contract_rejects_unknown_verdict(verdict: str) -> None:
    payload = _payload()
    payload["verdict"] = verdict
    with pytest.raises(DomainValidationError):
        ArchitectReview.from_dict(payload)


def test_changes_required_needs_actionable_findings() -> None:
    payload = _payload()
    payload["findings"] = []
    with pytest.raises(DomainValidationError, match="actionable findings"):
        ArchitectReview.from_dict(payload)


def test_review_contract_rejects_extra_fields() -> None:
    payload = _payload()
    payload["merge_now"] = True
    with pytest.raises(DomainValidationError, match="malformed"):
        ArchitectReview.from_dict(payload)


@pytest.mark.parametrize("kind", ["PRODUCT", "TECHNICAL"])
def test_human_decision_contract_strictly_parses_decision_kind(kind: str) -> None:
    payload = _payload()
    payload.update(
        verdict="HUMAN_DECISION_REQUIRED",
        findings=[],
        human_gate={
            "prompt": "Choose an approach",
            "resume_milestone_state": "ARCHITECT_REVIEW",
            "options": ["A", "B"],
            "test_instructions": None,
            "artifact_reference": None,
            "decision_kind": kind,
        },
    )
    assert ArchitectReview.from_dict(payload).human_gate is not None


@pytest.mark.parametrize(
    "resume_target", ["MERGE_READY", "CODING", "PREPARING_TASK", "REVIEW_REWORK"]
)
def test_human_decision_rejects_unsupported_resume_targets(
    resume_target: str,
) -> None:
    payload = _payload()
    payload.update(
        verdict="HUMAN_DECISION_REQUIRED",
        findings=[],
        human_gate={
            "prompt": "Choose an approach",
            "resume_milestone_state": resume_target,
            "options": ["A", "B"],
            "test_instructions": None,
            "artifact_reference": None,
            "decision_kind": "TECHNICAL",
        },
    )
    with pytest.raises(DomainValidationError, match="ARCHITECT_REVIEW"):
        ArchitectReview.from_dict(payload)


def test_human_test_rejects_decision_kind_and_non_review_resume() -> None:
    payload = _payload()
    payload.update(
        verdict="HUMAN_TEST_REQUIRED",
        findings=[],
        human_gate={
            "prompt": "Test it",
            "resume_milestone_state": "MERGE_READY",
            "options": [],
            "test_instructions": "Exercise the build",
            "artifact_reference": "artifact://build",
            "decision_kind": "PRODUCT",
        },
    )
    with pytest.raises(DomainValidationError, match="ARCHITECT_REVIEW"):
        ArchitectReview.from_dict(payload)


def test_openai_review_schema_only_allows_architect_review_resume() -> None:
    schema = cast(dict[str, Any], REVIEW_SCHEMA)
    human_gate = schema["properties"]["human_gate"]
    object_schema = human_gate["anyOf"][1]
    resume = object_schema["properties"]["resume_milestone_state"]
    assert resume == {"type": "string", "const": "ARCHITECT_REVIEW"}
