# mypy: disable-error-code="no-untyped-def,no-untyped-call,arg-type"
from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request

import pytest

from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.adapters.github.repository_names import GitHubHTTPResponse
from syntra_build.application.pull_requests import (
    PullRequestCreateConflict,
    PullRequestError,
    PullRequestFailure,
)
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.pull_requests import PullRequestCreateRequest
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


def test_create_422_is_a_body_independent_reconciliation_signal(tmp_path: Path) -> None:
    adapter = GitHubPullRequestAdapter(
        _config(tmp_path),
        transport=lambda r, t: GitHubHTTPResponse(
            422, b'{"message":"synthetic secret must not become evidence"}'
        ),
    )
    request = PullRequestCreateRequest(
        "1.0",
        "c",
        ProjectId.generate(),
        MilestoneId.generate(),
        77,
        "syntra/m22",
        "main",
        "a" * 40,
        "title",
        "body",
    )
    with pytest.raises(PullRequestCreateConflict) as conflict:
        adapter.create("owner/repo", request)
    assert "synthetic secret" not in str(conflict.value)


def _merge_request(strategy):
    from syntra_build.domain.merges import MERGE_INTERFACE_VERSION, MergeRequest

    return MergeRequest(
        MERGE_INTERFACE_VERSION,
        "corr",
        ProjectId.generate(),
        MilestoneId.generate(),
        77,
        12,
        "a" * 40,
        strategy,
        "gate-result",
    )


@pytest.mark.parametrize(
    ("strategy", "provider_value"),
    [
        ("SQUASH", "squash"),
        ("MERGE", "merge"),
        ("REBASE", "rebase"),
    ],
)
def test_merge_sends_exact_sha_and_strategy(
    tmp_path: Path, strategy: str, provider_value: str
) -> None:
    from syntra_build.domain.merges import MergeStatus, MergeStrategy

    seen: list[Request] = []

    def transport(request: Request, timeout: float) -> GitHubHTTPResponse:
        seen.append(request)
        return GitHubHTTPResponse(
            200, b'{"merged":true,"sha":"cccccccccccccccccccccccccccccccccccccccc"}'
        )

    result = GitHubPullRequestAdapter(_config(tmp_path), transport=transport).merge(
        "owner/repo", _merge_request(MergeStrategy(strategy))
    )
    payload = json.loads(seen[0].data or b"{}")
    assert seen[0].method == "PUT"
    assert payload == {"sha": "a" * 40, "merge_method": provider_value}
    assert result.status is MergeStatus.MERGED


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "REJECTED"),
        (409, "CONFLICT"),
        (422, "REJECTED"),
        (200, "NOT_MERGED"),
    ],
)
def test_merge_maps_known_provider_outcomes(
    tmp_path: Path, status: int, expected: str
) -> None:
    from syntra_build.domain.merges import MergeStrategy

    body = b'{"merged":false}'
    result = GitHubPullRequestAdapter(
        _config(tmp_path), transport=lambda _r, _t: GitHubHTTPResponse(status, body)
    ).merge("owner/repo", _merge_request(MergeStrategy.SQUASH))
    assert result.status.value == expected


@pytest.mark.parametrize(
    "transport",
    [
        lambda _r, _t: (_ for _ in ()).throw(TimeoutError()),
        lambda _r, _t: GitHubHTTPResponse(500, b"{}"),
        lambda _r, _t: GitHubHTTPResponse(200, b"not-json"),
    ],
)
def test_merge_timeout_5xx_and_malformed_response_are_ambiguous(
    tmp_path: Path, transport
) -> None:
    from syntra_build.application.provisioning import AmbiguousGitHubResult
    from syntra_build.domain.merges import MergeStrategy

    adapter = GitHubPullRequestAdapter(_config(tmp_path), transport=transport)
    with pytest.raises(AmbiguousGitHubResult):
        adapter.merge("owner/repo", _merge_request(MergeStrategy.SQUASH))
