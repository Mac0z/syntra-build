from dataclasses import replace
from datetime import UTC, datetime

from syntra_build.application.ci_status import format_ci_progress
from syntra_build.domain.ci import (
    CICheck,
    CICheckStatus,
    CIDiagnosis,
    CIOverallStatus,
    CIProgress,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId


def _progress() -> CIProgress:
    return CIProgress(
        "1.0",
        ProjectId.generate(),
        MilestoneId.generate(),
        12,
        "a" * 40,
        CIOverallStatus.UNKNOWN,
        (),
        diagnosis=CIDiagnosis.MISSING_WORKFLOW,
        diagnosis_summary="Approved workflow is absent at the exact PR head",
    )


def test_blocked_progress_names_exact_revision_and_operator_checks() -> None:
    progress = _progress()
    message = format_ci_progress(progress)
    assert progress.diagnosis_summary is not None
    assert progress.head_sha in message and "PR #12" in message
    assert "MISSING_WORKFLOW" in message and progress.diagnosis_summary in message
    assert "Human action is required" in message
    assert "CI monitoring is blocked" in message
    assert "No human action" not in message
    assert "approved workflow" in message and "triggers and filters" in message
    assert "GitHub Actions is enabled" in message and "GitHub access" in message


def test_delayed_progress_stays_pending_and_gates_architect_review() -> None:
    next_check = datetime(2026, 10, 11, tzinfo=UTC)
    message = format_ci_progress(replace(_progress(), next_check_at=next_check))
    assert f"recheck at {next_check.isoformat()}" in message
    assert "Architect review remains gated" in message
    assert "Human action is required" not in message


def test_provider_diagnosis_does_not_claim_missing_workflows() -> None:
    message = format_ci_progress(
        replace(
            _progress(),
            diagnosis=CIDiagnosis.PROVIDER_AUTHENTICATION,
            diagnosis_summary="GitHub API authentication failed",
            required_checks=(CICheck("tests", "old-check", CICheckStatus.RUNNING),),
        )
    )
    assert "PROVIDER_AUTHENTICATION" in message
    assert "GitHub API availability" in message and "GitHub access" in message
    assert "approved workflow" not in message
    assert "Last observed checks: tests: RUNNING" in message
