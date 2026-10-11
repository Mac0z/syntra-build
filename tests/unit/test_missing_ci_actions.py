from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from syntra_build.adapters.github.actions import (
    CIProviderError,
    CIProviderFailure,
    GitHubActionsAdapter,
)
from syntra_build.adapters.github.repository_names import GitHubHTTPResponse
from syntra_build.domain.ci import (
    CIObservation,
    CIOverallStatus,
    CIWorkflowPresence,
)
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    SecretValue,
    load_config,
)

HEAD = "a" * 40
PR_NUMBER = 31


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
        secrets=SecretInputs(github_token=SecretValue("fake-ci-token")),
    )


def _response(payload: object) -> GitHubHTTPResponse:
    return GitHubHTTPResponse(200, json.dumps(payload).encode())


def _workflow_file(name: str = "ci.yml") -> dict[str, object]:
    return {"name": name, "path": f".github/workflows/{name}", "type": "file"}


def _run(status: object = "queued", **overrides: object) -> dict[str, object]:
    return {
        "id": 9,
        "head_sha": HEAD,
        "event": "pull_request",
        "pull_requests": [{"number": PR_NUMBER}],
        "status": status,
        **overrides,
    }


def _observe(
    tmp_path: Path,
    *,
    runs: tuple[Mapping[str, object], ...] = (),
    contents: GitHubHTTPResponse | None = None,
    root_contents: GitHubHTTPResponse | None = None,
    jobs: GitHubHTTPResponse | None = None,
) -> tuple[CIObservation, list[Request]]:
    requests: list[Request] = []

    def transport(request: Request, timeout: float) -> GitHubHTTPResponse:
        requests.append(request)
        path = urlsplit(request.full_url).path
        if path.endswith("/contents/.github/workflows"):
            return contents if contents is not None else _response([_workflow_file()])
        if path.endswith("/contents"):
            return root_contents if root_contents is not None else _response([])
        if path.endswith("/jobs"):
            return jobs if jobs is not None else _response({"jobs": []})
        assert path == "/repos/owner/repo/actions/runs"
        return _response({"workflow_runs": runs})

    observation = GitHubActionsAdapter(_config(tmp_path), transport=transport).observe(
        "owner/repo", PR_NUMBER, HEAD
    )
    return observation, requests


@pytest.mark.parametrize(
    ("contents", "presence"),
    [
        (GitHubHTTPResponse(404, b"{}"), CIWorkflowPresence.MISSING),
        (_response([]), CIWorkflowPresence.MISSING),
        (_response([_workflow_file("README.md")]), CIWorkflowPresence.MISSING),
        (
            _response(
                [{"name": "nested", "path": ".github/workflows/nested", "type": "dir"}]
            ),
            CIWorkflowPresence.MISSING,
        ),
        (_response([_workflow_file("ci.yml")]), CIWorkflowPresence.CONFIGURED),
        (_response([_workflow_file("ci.yaml")]), CIWorkflowPresence.CONFIGURED),
    ],
)
def test_no_runs_discovers_workflow_files_at_exact_head(
    tmp_path: Path, contents: GitHubHTTPResponse, presence: CIWorkflowPresence
) -> None:
    observation, requests = _observe(tmp_path, contents=contents)

    assert observation == CIObservation((), (), presence, None)
    assert len(requests) == (3 if contents.status == 404 else 2)
    run_query = parse_qs(urlsplit(requests[0].full_url).query)
    assert run_query["head_sha"] == [HEAD]
    assert run_query["event"] == ["pull_request"]
    assert urlsplit(requests[1].full_url).path.endswith("/contents/.github/workflows")
    assert parse_qs(urlsplit(requests[1].full_url).query) == {"ref": [HEAD]}
    if contents.status == 404:
        assert urlsplit(requests[2].full_url).path == "/repos/owner/repo/contents"
        assert parse_qs(urlsplit(requests[2].full_url).query) == {"ref": [HEAD]}
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("queued", CIOverallStatus.QUEUED),
        ("waiting", CIOverallStatus.QUEUED),
        ("requested", CIOverallStatus.QUEUED),
        ("pending", CIOverallStatus.QUEUED),
        ("in_progress", CIOverallStatus.RUNNING),
        ("completed", None),
        ("unknown", None),
        (None, None),
    ],
)
def test_bound_run_without_jobs_reports_startup_only(
    tmp_path: Path, status: object, expected: CIOverallStatus | None
) -> None:
    observation, requests = _observe(
        tmp_path, runs=(_run(status, conclusion="success"),)
    )

    assert observation.checks == ()
    assert observation.external_workflow_run_ids == ("9",)
    assert observation.workflow_presence is CIWorkflowPresence.CONFIGURED
    assert observation.workflow_status is expected
    assert sum("/jobs?" in request.full_url for request in requests) == 1


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", None])
def test_completed_workflow_conclusion_never_invents_required_job_evidence(
    tmp_path: Path, conclusion: str | None
) -> None:
    observation, _ = _observe(
        tmp_path, runs=(_run("completed", conclusion=conclusion),)
    )

    assert observation.checks == ()
    assert observation.workflow_status is None


def test_running_workflow_takes_precedence_over_queued_startup(tmp_path: Path) -> None:
    observation, _ = _observe(
        tmp_path, runs=(_run("queued"), _run("in_progress", id=10))
    )

    assert observation.workflow_status is CIOverallStatus.RUNNING
    assert observation.external_workflow_run_ids == ("9", "10")


@pytest.mark.parametrize(
    "overrides",
    [
        {"head_sha": "b" * 40},
        {"event": "push"},
        {"pull_requests": [{"number": 99}]},
        {"pull_requests": []},
    ],
)
def test_endpoint_filters_are_independently_checked_before_startup_evidence(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    observation, requests = _observe(tmp_path, runs=(_run("in_progress", **overrides),))

    assert observation.external_workflow_run_ids == ()
    assert observation.workflow_status is None
    assert observation.workflow_presence is CIWorkflowPresence.CONFIGURED
    assert not any("/jobs?" in request.full_url for request in requests)


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (GitHubHTTPResponse(401, b"{}"), CIProviderFailure.AUTHENTICATION),
        (GitHubHTTPResponse(403, b"{}"), CIProviderFailure.AUTHENTICATION),
        (GitHubHTTPResponse(429, b"{}"), CIProviderFailure.TRANSIENT),
        (GitHubHTTPResponse(503, b"{}"), CIProviderFailure.TRANSIENT),
        (GitHubHTTPResponse(422, b"{}"), CIProviderFailure.REJECTION),
        (GitHubHTTPResponse(200, b"malformed"), CIProviderFailure.MALFORMED),
        (_response({"name": "ci.yml"}), CIProviderFailure.MALFORMED),
        (_response([None]), CIProviderFailure.MALFORMED),
        (_response([{"name": "ci.yml"}]), CIProviderFailure.MALFORMED),
        (
            _response([{**_workflow_file(), "path": ".github/workflows-other/ci.yml"}]),
            CIProviderFailure.MALFORMED,
        ),
        (
            _response([{**_workflow_file(), "type": {"malformed": True}}]),
            CIProviderFailure.MALFORMED,
        ),
    ],
)
def test_contents_provider_failure_cannot_become_missing_infrastructure(
    tmp_path: Path, response: GitHubHTTPResponse, failure: CIProviderFailure
) -> None:
    with pytest.raises(CIProviderError) as caught:
        _observe(tmp_path, contents=response)
    assert caught.value.failure is failure


@pytest.mark.parametrize(
    ("root_response", "failure"),
    [
        (GitHubHTTPResponse(401, b"{}"), CIProviderFailure.AUTHENTICATION),
        (GitHubHTTPResponse(403, b"{}"), CIProviderFailure.AUTHENTICATION),
        (GitHubHTTPResponse(404, b"{}"), CIProviderFailure.REJECTION),
        (GitHubHTTPResponse(429, b"{}"), CIProviderFailure.TRANSIENT),
        (GitHubHTTPResponse(503, b"{}"), CIProviderFailure.TRANSIENT),
        (GitHubHTTPResponse(200, b"malformed"), CIProviderFailure.MALFORMED),
        (_response({"message": "unreadable"}), CIProviderFailure.MALFORMED),
        (_response([None]), CIProviderFailure.MALFORMED),
        (_response([{"name": ".github"}]), CIProviderFailure.MALFORMED),
        (_response([_workflow_file()]), CIProviderFailure.MALFORMED),
    ],
)
def test_workflow_directory_404_cannot_prove_absence_without_contents_access(
    tmp_path: Path,
    root_response: GitHubHTTPResponse,
    failure: CIProviderFailure,
) -> None:
    with pytest.raises(CIProviderError) as caught:
        _observe(
            tmp_path,
            contents=GitHubHTTPResponse(404, b"{}"),
            root_contents=root_response,
        )
    assert caught.value.failure is failure


def test_readable_exact_head_root_proves_workflow_directory_absence(
    tmp_path: Path,
) -> None:
    observation, requests = _observe(
        tmp_path,
        contents=GitHubHTTPResponse(404, b"{}"),
        root_contents=_response(
            [{"name": ".github", "path": ".github", "type": "dir"}]
        ),
    )

    assert observation.workflow_presence is CIWorkflowPresence.MISSING
    assert observation.checks == () and observation.external_workflow_run_ids == ()
    assert parse_qs(urlsplit(requests[-1].full_url).query) == {"ref": [HEAD]}


def test_root_contents_transport_failure_is_provider_failure(tmp_path: Path) -> None:
    def transport(request: Request, timeout: float) -> GitHubHTTPResponse:
        path = urlsplit(request.full_url).path
        if path.endswith("/actions/runs"):
            return _response({"workflow_runs": []})
        if path.endswith("/contents/.github/workflows"):
            return GitHubHTTPResponse(404, b"{}")
        assert path.endswith("/contents")
        raise TimeoutError("injected private provider transport details")

    adapter = GitHubActionsAdapter(_config(tmp_path), transport=transport)
    with pytest.raises(CIProviderError) as caught:
        adapter.observe("owner/repo", PR_NUMBER, HEAD)

    assert caught.value.failure is CIProviderFailure.TRANSIENT
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (401, CIProviderFailure.AUTHENTICATION),
        (403, CIProviderFailure.AUTHENTICATION),
        (404, CIProviderFailure.REJECTION),
        (429, CIProviderFailure.TRANSIENT),
        (500, CIProviderFailure.TRANSIENT),
    ],
)
def test_failed_run_discovery_does_not_even_request_workflow_files(
    tmp_path: Path, status: int, failure: CIProviderFailure
) -> None:
    requests: list[Request] = []

    def transport(request: Request, timeout: float) -> GitHubHTTPResponse:
        requests.append(request)
        return GitHubHTTPResponse(status, b"{}")

    adapter = GitHubActionsAdapter(_config(tmp_path), transport=transport)
    with pytest.raises(CIProviderError) as caught:
        adapter.observe("owner/repo", PR_NUMBER, HEAD)

    assert caught.value.failure is failure
    assert len(requests) == 1
    assert "/actions/runs?" in requests[0].full_url


def test_failed_job_discovery_cannot_become_missing_workflow(tmp_path: Path) -> None:
    with pytest.raises(CIProviderError) as caught:
        _observe(tmp_path, runs=(_run(),), jobs=GitHubHTTPResponse(503, b"{}"))
    assert caught.value.failure is CIProviderFailure.TRANSIENT


@pytest.mark.parametrize("number", [None, False, 0, "31", {"number": 31}])
def test_malformed_pr_binding_is_provider_failure(
    tmp_path: Path, number: object
) -> None:
    with pytest.raises(CIProviderError) as caught:
        _observe(tmp_path, runs=(_run(pull_requests=[{"number": number}]),))
    assert caught.value.failure is CIProviderFailure.MALFORMED


def test_real_jobs_preserve_observation_without_contents_dependency(
    tmp_path: Path,
) -> None:
    observation, requests = _observe(
        tmp_path,
        runs=(_run("completed"),),
        jobs=_response(
            {
                "jobs": [
                    {
                        "id": 1,
                        "name": "validate",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        ),
        contents=GitHubHTTPResponse(401, b"{}"),
    )

    assert len(observation.checks) == 1
    assert observation.workflow_presence is CIWorkflowPresence.UNKNOWN
    assert observation.workflow_status is None
    assert len(requests) == 2
    assert not any("/contents/" in request.full_url for request in requests)


def test_observation_defaults_preserve_existing_injected_providers() -> None:
    observation = CIObservation((), ("run-1",))
    assert observation.workflow_presence is CIWorkflowPresence.UNKNOWN
    assert observation.workflow_status is None
