from datetime import UTC, datetime
from uuid import uuid4

import pytest

from syntra_build.domain.errors import DomainValidationError
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.merges import (
    MERGE_INTERFACE_VERSION,
    MergeEligibilityResult,
    MergeEvidence,
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
            MERGE_INTERFACE_VERSION,
            "result",
            "correlation",
            _project(),
            _milestone(),
            42,
            7,
            "a" * 40,
            True,
            (MergeGuardResult("required_ci", False, "current_ci_not_passed"),),
            MergeEvidence(None, None, None, None),
            datetime.now(UTC),
        )


def test_eligibility_contracts_reject_unknown_versions() -> None:
    from syntra_build.domain.merges import MergeEligibilityRequest

    with pytest.raises(DomainValidationError):
        MergeEligibilityRequest(
            "2.0",
            "correlation",
            _project(),
            _milestone(),
            "repo",
            42,
            "pr",
            7,
            "head",
            "main",
            "a" * 40,
        )


@pytest.mark.parametrize("value", ["MERGED", "UNKNOWN", "REJECTED"])
def test_merge_status_rejects_unknown_values(value: str) -> None:
    assert MergeStatus(value).value == value
    with pytest.raises(ValueError):
        MergeStatus("provider_custom_status")
