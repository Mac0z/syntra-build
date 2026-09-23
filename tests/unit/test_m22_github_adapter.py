from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request

import pytest

from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.adapters.github.repository_names import GitHubHTTPResponse
from syntra_build.application.pull_requests import PullRequestError, PullRequestFailure
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)


def _config(tmp_path: Path) -> ApplicationConfig:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "app"),
                "configuration_root": str(tmp_path / "config"),
                "data_root": str(data),
                "log_root": str(tmp_path / "logs"),
            },
            "database": {"sqlite_path": str(data / "syntra.db")},
            "github": {"enabled": True, "owner": "owner"},
        },
        environ={},
        secrets=SecretInputs(github_token=SecretValue("synthetic-token")),
    )


def _payload() -> dict[str, object]:
    repo = {"id": 77, "full_name": "owner/repo"}
    return {
        "number": 12,
        "state": "open",
        "merged": False,
        "head": {"ref": "syntra/m22", "sha": "a" * 40, "repo": repo},
        "base": {"ref": "main", "repo": repo},
        "html_url": "https://github.com/owner/repo/pull/12",
        "merged_at": None,
        "closed_at": None,
        "merge_commit_sha": None,
    }


def test_adapter_normalizes_without_exposing_token(tmp_path: Path) -> None:
    seen: list[Request] = []

    def transport(request: Request, timeout: float) -> GitHubHTTPResponse:
        seen.append(request)
        return GitHubHTTPResponse(200, json.dumps(_payload()).encode())

    adapter = GitHubPullRequestAdapter(_config(tmp_path), transport=transport)
    result = adapter.get("owner/repo", 12, ProjectId.generate(), MilestoneId.generate())
    assert result.pull_request_number == 12 and result.repository_id == 77
    assert "top-secret" not in repr(result)


def test_adapter_rejects_malformed_and_normalizes_authentication(
    tmp_path: Path,
) -> None:
    project, milestone = ProjectId.generate(), MilestoneId.generate()
    adapter = GitHubPullRequestAdapter(
        _config(tmp_path), transport=lambda r, t: GitHubHTTPResponse(200, b"{}")
    )
    with pytest.raises(PullRequestError) as malformed:
        adapter.get("owner/repo", 12, project, milestone)
    assert malformed.value.failure is PullRequestFailure.TRANSIENT
    adapter = GitHubPullRequestAdapter(
        _config(tmp_path), transport=lambda r, t: GitHubHTTPResponse(401, b"{}")
    )
    with pytest.raises(PullRequestError) as auth:
        adapter.get("owner/repo", 12, project, milestone)
    assert auth.value.failure is PullRequestFailure.AUTHENTICATION
    assert "token" not in str(auth.value)
