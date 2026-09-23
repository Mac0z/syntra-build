# ruff: noqa: E501
"""Conservative required-check and failure-classification policy."""

from __future__ import annotations

from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIFailureClassification,
    CIOverallStatus,
)


class RequiredCheckPolicy:
    """Version-one policy: every applicable Actions job is required."""

    def evaluate(self, checks: tuple[CICheck, ...]) -> CIOverallStatus:
        if not checks:
            return CIOverallStatus.UNKNOWN
        if any(item.status is CICheckStatus.QUEUED for item in checks):
            return CIOverallStatus.QUEUED
        if any(item.status is CICheckStatus.RUNNING for item in checks):
            return CIOverallStatus.RUNNING
        conclusions = {item.conclusion for item in checks}
        if conclusions == {CICheckConclusion.PASSED}:
            return CIOverallStatus.PASSED
        if CICheckConclusion.FAILED in conclusions:
            return CIOverallStatus.FAILED
        if CICheckConclusion.CANCELLED in conclusions:
            return CIOverallStatus.CANCELLED
        # Skipped, neutral, missing and provider-unknown conclusions fail closed.
        return CIOverallStatus.UNKNOWN

    def classify(self, checks: tuple[CICheck, ...]) -> CIFailureClassification:
        summaries = " ".join(
            f"{item.name} {item.failure_summary or ''}"
            for item in checks
            if item.conclusion is CICheckConclusion.FAILED
        ).casefold()
        if any(word in summaries for word in ("pytest", "test failed", "tests failed")):
            return CIFailureClassification.TEST
        if any(
            word in summaries for word in ("syntax", "compile", "lint", "type check")
        ):
            return CIFailureClassification.IMPLEMENTATION
        if any(word in summaries for word in ("configuration", "workflow invalid")):
            return CIFailureClassification.CONFIGURATION
        if any(
            word in summaries
            for word in ("runner lost", "hosted runner", "internal error")
        ):
            return CIFailureClassification.TRANSIENT_INFRASTRUCTURE
        if any(
            word in summaries
            for word in ("rate limit", "service unavailable", "dependency")
        ):
            return CIFailureClassification.EXTERNAL_DEPENDENCY
        return CIFailureClassification.UNKNOWN
