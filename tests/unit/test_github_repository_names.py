from __future__ import annotations

from pathlib import Path
from urllib.request import Request

import pytest

from syntra_build.adapters.github import (
    GitHubHTTPResponse,
    GitHubRepositoryNameConflictChecker,
)
from syntra_build.application.projects import RepositoryConflictCheckUnavailable
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)


def config(tmp_path: Path) -> ApplicationConfig:
    data = tmp_path / "data"
    data.mkdir()
    return load_config(
        {
            "filesystem": {
                "application_root": str(tmp_path / "app"),
                "configuration_root": str(tmp_path / "config"),
                "data_root": str(data),
                "log_root": str(tmp_path / "logs"),
            },
            "database": {"sqlite_path": str(data / "syntra.db")},
            "github": {"enabled": True, "owner": "Mac0z"},
        },
        environ={},
        secrets=SecretInputs(github_token=SecretValue("synthetic-github-token")),
    )


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (GitHubHTTPResponse(200, b'{"full_name":"Mac0z/flow-track"}'), True),
        (GitHubHTTPResponse(404, b'{"message":"Not Found"}'), False),
    ],
)
def test_exact_repository_lookup_distinguishes_exists_and_absent(
    tmp_path: Path, response: GitHubHTTPResponse, expected: bool
) -> None:
    requests: list[Request] = []

    def transport(request: Request, timeout: float) -> GitHubHTTPResponse:
        requests.append(request)
        assert timeout == 30.0
        return response

    checker = GitHubRepositoryNameConflictChecker(config(tmp_path), transport=transport)
    assert checker.conflicts("flow-track") is expected
    assert requests[0].method == "GET"
    assert requests[0].full_url.endswith("/repos/Mac0z/flow-track")
    assert "synthetic-github-token" not in requests[0].full_url


@pytest.mark.parametrize(
    "response",
    [
        GitHubHTTPResponse(401, b"{}"),
        GitHubHTTPResponse(403, b"{}"),
        GitHubHTTPResponse(429, b"{}"),
        GitHubHTTPResponse(500, b"{}"),
        GitHubHTTPResponse(200, b"[]"),
        GitHubHTTPResponse(200, b"not-json"),
        GitHubHTTPResponse(200, b'{"full_name":"other/repository"}'),
    ],
)
def test_inconclusive_provider_responses_fail_closed(
    tmp_path: Path, response: GitHubHTTPResponse
) -> None:
    checker = GitHubRepositoryNameConflictChecker(
        config(tmp_path), transport=lambda _request, _timeout: response
    )
    with pytest.raises(RepositoryConflictCheckUnavailable):
        checker.conflicts("flow-track")


def test_network_failure_fails_closed(tmp_path: Path) -> None:
    def timeout(_request: Request, _seconds: float) -> GitHubHTTPResponse:
        raise TimeoutError("synthetic timeout")

    checker = GitHubRepositoryNameConflictChecker(config(tmp_path), transport=timeout)
    with pytest.raises(RepositoryConflictCheckUnavailable):
        checker.conflicts("flow-track")
