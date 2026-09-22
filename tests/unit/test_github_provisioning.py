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
