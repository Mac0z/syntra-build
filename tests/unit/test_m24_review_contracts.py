from __future__ import annotations

import pytest

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
