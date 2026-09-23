from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request

import pytest

from syntra_build.adapters.github.actions import (
    CIProviderError,
    CIProviderFailure,
    GitHubActionsAdapter,
)
from syntra_build.adapters.github.repository_names import GitHubHTTPResponse
from syntra_build.application.ci_policy import RequiredCheckPolicy
from syntra_build.application.ci_polling import (
    AdaptivePollingPolicy,
    InfrastructureRetryPolicy,
)
from syntra_build.application.ci_status import format_ci_progress
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIOverallStatus,
    CIProgress,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)


def _check(
    status: CICheckStatus, conclusion: CICheckConclusion | None = None
) -> CICheck:
    return CICheck("validate", "1", status, conclusion)


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ((), CIOverallStatus.UNKNOWN),
        ((_check(CICheckStatus.QUEUED),), CIOverallStatus.QUEUED),
        ((_check(CICheckStatus.RUNNING),), CIOverallStatus.RUNNING),
        (
            (_check(CICheckStatus.COMPLETED, CICheckConclusion.PASSED),),
            CIOverallStatus.PASSED,
        ),
        (
            (_check(CICheckStatus.COMPLETED, CICheckConclusion.FAILED),),
            CIOverallStatus.FAILED,
        ),
        (
            (_check(CICheckStatus.COMPLETED, CICheckConclusion.CANCELLED),),
            CIOverallStatus.CANCELLED,
        ),
        (
            (_check(CICheckStatus.COMPLETED, CICheckConclusion.SKIPPED),),
            CIOverallStatus.UNKNOWN,
        ),
        (
            (_check(CICheckStatus.COMPLETED, CICheckConclusion.NEUTRAL),),
            CIOverallStatus.UNKNOWN,
        ),
    ],
)
def test_required_policy_fails_closed(
    checks: tuple[CICheck, ...], expected: CIOverallStatus
) -> None:
    assert RequiredCheckPolicy().evaluate(checks) is expected


def _config(tmp_path: Path) -> ApplicationConfig:
    data = tmp_path / "data"
    data.mkdir()
    return load_config(
        {
            "filesystem": {
                "application_root": tmp_path / "app",
                "configuration_root": tmp_path / "config",
                "data_root": data,
                "log_root": tmp_path / "logs",
            },
            "database": {"sqlite_path": data / "db"},
            "github": {"enabled": True, "owner": "owner"},
        },
        environ={},
        secrets=SecretInputs(github_token=SecretValue("fake-token")),
    )


def test_actions_adapter_exact_head_multiple_jobs_and_pagination(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def transport(request: Request, timeout: float) -> GitHubHTTPResponse:
        seen.append(request.full_url)
        if "/jobs" in request.full_url:
            jobs = [
                {
                    "id": 1,
                    "name": "validate",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://example/1",
                },
                {"id": 2, "name": "tests", "status": "in_progress", "conclusion": None},
            ]
            return GitHubHTTPResponse(200, json.dumps({"jobs": jobs}).encode())
        runs = [
            {"id": 9, "head_sha": "a" * 40, "event": "pull_request"},
            {"id": 10, "head_sha": "b" * 40, "event": "pull_request"},
        ]
        return GitHubHTTPResponse(200, json.dumps({"workflow_runs": runs}).encode())

    observed = GitHubActionsAdapter(_config(tmp_path), transport=transport).observe(
        "owner/repo", "a" * 40
    )
    assert [item.name for item in observed.checks] == ["validate", "tests"]
    assert observed.external_workflow_run_ids == ("9",)
    assert all("head_sha=" + "a" * 40 in url for url in seen if "/actions/runs?" in url)


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (GitHubHTTPResponse(401, b"{}"), CIProviderFailure.AUTHENTICATION),
        (GitHubHTTPResponse(500, b"{}"), CIProviderFailure.TRANSIENT),
        (GitHubHTTPResponse(422, b"{}"), CIProviderFailure.REJECTION),
        (GitHubHTTPResponse(200, b"not-json"), CIProviderFailure.MALFORMED),
    ],
)
def test_actions_adapter_classifies_provider_failures(
    tmp_path: Path, response: GitHubHTTPResponse, failure: CIProviderFailure
) -> None:
    adapter = GitHubActionsAdapter(_config(tmp_path), transport=lambda r, t: response)
    with pytest.raises(CIProviderError) as caught:
        adapter.observe("owner/repo", "a" * 40)
    assert caught.value.failure is failure


def test_polling_and_retry_are_pure_and_bounded() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 1, 1, tzinfo=UTC)
    polling = AdaptivePollingPolicy(
        initial_seconds=1,
        medium_seconds=2,
        long_seconds=3,
        medium_after_seconds=10,
        long_after_seconds=20,
    )
    assert polling.next_at(now, now) == now + timedelta(seconds=1)
    assert polling.next_at(now, now + timedelta(seconds=30)) == now + timedelta(
        seconds=33
    )
    retry = InfrastructureRetryPolicy((5, 30))
    assert retry.next_at(0, now) == now + timedelta(seconds=5)
    assert retry.next_at(2, now) is None


def test_status_projection_includes_each_required_check() -> None:
    progress = CIProgress(
        "1.0",
        ProjectId.generate(),
        MilestoneId.generate(),
        31,
        "a" * 40,
        CIOverallStatus.RUNNING,
        (
            CICheck("validate", "1", CICheckStatus.RUNNING),
            CICheck(
                "tests",
                "2",
                CICheckStatus.COMPLETED,
                CICheckConclusion.PASSED,
            ),
        ),
    )
    message = format_ci_progress(progress)
    assert "PR #31" in message and "validate: RUNNING" in message
    assert "tests: PASSED" in message and "No human action" in message
