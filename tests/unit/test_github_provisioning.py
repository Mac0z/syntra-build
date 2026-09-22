import base64
import json
from pathlib import Path

import pytest

from syntra_build.adapters.github.provisioning import GitHubProvisioningAdapter
from syntra_build.adapters.github.repository_names import GitHubHTTPResponse
from syntra_build.application.provisioning import (
    AmbiguousGitHubResult,
    ProvisioningError,
    ProvisioningFailure,
)
from syntra_build.domain import RepositoryVisibility
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)


def _config(tmp_path: Path) -> ApplicationConfig:
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
        secrets=SecretInputs(github_token=SecretValue("synthetic-token")),
    )


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (401, ProvisioningFailure.AUTHENTICATION),
        (403, ProvisioningFailure.AUTHENTICATION),
        (422, ProvisioningFailure.COLLISION),
        (400, ProvisioningFailure.PROVIDER_REJECTION),
    ],
)
def test_definite_create_rejections_are_not_ambiguous(
    tmp_path: Path, status: int, failure: ProvisioningFailure
) -> None:
    adapter = GitHubProvisioningAdapter(
        _config(tmp_path),
        transport=lambda _request, _timeout: GitHubHTTPResponse(status, b"{}"),
    )
    with pytest.raises(ProvisioningError) as raised:
        adapter.create_repository("example", RepositoryVisibility.PUBLIC)
    assert raised.value.failure is failure


def test_uncertain_create_transport_result_is_ambiguous(tmp_path: Path) -> None:
    def timeout(*_args: object) -> GitHubHTTPResponse:
        raise TimeoutError("request outcome unknown")

    adapter = GitHubProvisioningAdapter(_config(tmp_path), transport=timeout)
    with pytest.raises(AmbiguousGitHubResult):
        adapter.create_repository("example", RepositoryVisibility.PUBLIC)


def test_empty_repository_means_main_does_not_exist(tmp_path: Path) -> None:
    response = GitHubHTTPResponse(
        409,
        b'{"message":"Git Repository is empty.",'
        b'"documentation_url":"https://docs.github.com/rest/git/refs",'
        b'"status":"409"}',
    )
    adapter = GitHubProvisioningAdapter(
        _config(tmp_path), transport=lambda _request, _timeout: response
    )

    assert adapter.main_sha("Mac0z", "empty-repository") is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"message":"Conflict"}',
        b'{"message":"Git Repository is empty"}',
        b'{"message":"git repository is empty."}',
        b"{}",
    ],
)
def test_other_conflicts_do_not_mean_main_is_absent(
    tmp_path: Path, body: bytes
) -> None:
    adapter = GitHubProvisioningAdapter(
        _config(tmp_path),
        transport=lambda _request, _timeout: GitHubHTTPResponse(409, body),
    )

    with pytest.raises(ProvisioningError) as raised:
        adapter.main_sha("Mac0z", "conflicted-repository")
    assert raised.value.failure is ProvisioningFailure.TRANSIENT


def _content_adapter(
    tmp_path: Path, payload: object, *, status: int = 200
) -> GitHubProvisioningAdapter:
    response = GitHubHTTPResponse(status, json.dumps(payload).encode())
    return GitHubProvisioningAdapter(
        _config(tmp_path), transport=lambda _request, _timeout: response
    )


@pytest.mark.parametrize(
    "encoded",
    [
        base64.b64encode(b"approved baseline\n").decode("ascii"),
        "YXBwcm92ZWQg\nYmFzZWxpbmUK\n",
        "\tYXBw cm92\r\nZWQg\fYmFzZWxpbmUK\v",
    ],
    ids=["unwrapped", "github-newlines", "permitted-ascii-whitespace"],
)
def test_file_content_strips_only_ascii_whitespace_before_strict_decode(
    tmp_path: Path, encoded: str
) -> None:
    adapter = _content_adapter(tmp_path, {"encoding": "base64", "content": encoded})

    assert (
        adapter.file_content("Mac0z", "example", "a" * 40, "SPEC.md")
        == b"approved baseline\n"
    )


@pytest.mark.parametrize(
    "content",
    ["YXBwcm92ZWQ*", "YXBwcm92ZWQ===", "YXBwcm92Z", "YWJj\u00a0ZA=="],
)
def test_file_content_rejects_malformed_non_whitespace_base64(
    tmp_path: Path, content: str
) -> None:
    adapter = _content_adapter(tmp_path, {"encoding": "base64", "content": content})

    with pytest.raises(ProvisioningError) as raised:
        adapter.file_content("Mac0z", "example", "a" * 40, "SPEC.md")
    assert raised.value.failure is ProvisioningFailure.TRANSIENT


@pytest.mark.parametrize(
    "payload",
    [
        {"encoding": "utf-8", "content": "YQ=="},
        {"content": "YQ=="},
        {"encoding": "base64"},
        {"encoding": "base64", "content": 123},
    ],
)
def test_file_content_rejects_incorrect_or_missing_encoding_fields(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    adapter = _content_adapter(tmp_path, payload)

    with pytest.raises(ProvisioningError) as raised:
        adapter.file_content("Mac0z", "example", "a" * 40, "AGENTS.md")
    assert raised.value.failure is ProvisioningFailure.TRANSIENT
