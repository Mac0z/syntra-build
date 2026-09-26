from datetime import UTC, datetime
from uuid import uuid4

import pytest

from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.merges import (
    MERGE_INTERFACE_VERSION,
    MergeEligibilityResult,
    MergeGuardResult,
    MergeRequest,
    MergeStatus,
    MergeStrategy,
)


def _project() -> ProjectId:
    return ProjectId.from_string(str(uuid4()))


def _milestone() -> MilestoneId:
    return MilestoneId.from_string(str(uuid4()))


def test_merge_request_requires_typed_strategy_and_exact_sha() -> None:
    request = MergeRequest(
        MERGE_INTERFACE_VERSION,
        "correlation",
        _project(),
        _milestone(),
        42,
        7,
        "a" * 40,
        MergeStrategy.SQUASH,
        "gatekeeper-result",
    )
    assert request.merge_strategy is MergeStrategy.SQUASH
    with pytest.raises(DomainValidationError):
        MergeRequest(
            MERGE_INTERFACE_VERSION,
            "correlation",
            request.project_id,
            request.milestone_id,
            42,
            7,
            "a" * 40,
            "SQUASH",  # type: ignore[arg-type]
            "gatekeeper-result",
        )


def test_eligibility_cannot_disagree_with_individual_guards() -> None:
    with pytest.raises(DomainValidationError):
        MergeEligibilityResult(
            "result",
            "correlation",
            _project(),
            _milestone(),
            42,
            7,
            "a" * 40,
            True,
            (MergeGuardResult("required_ci", False, "current_ci_not_passed"),),
            datetime.now(UTC),
        )


@pytest.mark.parametrize("value", ["MERGED", "UNKNOWN", "REJECTED"])
def test_merge_status_rejects_unknown_values(value: str) -> None:
    assert MergeStatus(value).value == value
    with pytest.raises(ValueError):
        MergeStatus("provider_custom_status")
