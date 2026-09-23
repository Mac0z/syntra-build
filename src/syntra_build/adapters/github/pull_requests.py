"""Authenticated GitHub REST boundary for pull requests."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import quote
from urllib.request import Request

from syntra_build.adapters.github.repository_names import (
    GitHubTransport,
    _stdlib_transport,
)
from syntra_build.application.provisioning import AmbiguousGitHubResult
from syntra_build.application.pull_requests import PullRequestError, PullRequestFailure
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.domain.pull_requests import (
    PULL_REQUEST_INTERFACE_VERSION,
    PullRequestCreateRequest,
    PullRequestDescriptor,
    PullRequestState,
)
from syntra_build.infrastructure.config import ApplicationConfig

_API_ROOT = "https://api.github.com"


class GitHubPullRequestAdapter:
    def __init__(
        self,
        config: ApplicationConfig,
        *,
        transport: GitHubTransport = _stdlib_transport,
    ) -> None:
        token = config.secrets.github_token
        if not config.github.enabled or token is None:
            raise PullRequestError(
                PullRequestFailure.AUTHENTICATION,
                "GitHub pull requests are not configured",
            )
        self._token = token.value
        self._timeout, self._transport = config.github.api_timeout_seconds, transport

    def _request(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> tuple[int, object | None]:
        request = Request(
            f"{_API_ROOT}{path}",
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "syntra-build",
                "Content-Type": "application/json",
            },
        )
        try:
            response = self._transport(request, self._timeout)
        except Exception as error:
            if method == "POST":
                raise AmbiguousGitHubResult(
                    "GitHub mutation result is ambiguous"
                ) from error
            raise PullRequestError(
                PullRequestFailure.TRANSIENT, "GitHub pull request lookup failed"
            ) from error
        if response.status in {401, 403}:
            raise PullRequestError(
                PullRequestFailure.AUTHENTICATION, "GitHub rejected pull request access"
            )
        if response.status >= 500:
            failure = (
                PullRequestFailure.AMBIGUOUS
                if method == "POST"
                else PullRequestFailure.TRANSIENT
            )
            if method == "POST":
                raise AmbiguousGitHubResult("GitHub mutation result is ambiguous")
            raise PullRequestError(failure, "GitHub is temporarily unavailable")
        try:
            return response.status, json.loads(response.body) if response.body else None
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            failure = (
                PullRequestFailure.AMBIGUOUS
                if method == "POST"
                else PullRequestFailure.TRANSIENT
            )
            if method == "POST":
                raise AmbiguousGitHubResult(
                    "GitHub mutation response was malformed"
                ) from error
            raise PullRequestError(failure, "GitHub response was malformed") from error

    def find_open(
        self,
        repository_full_name: str,
        head_branch: str,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> Sequence[PullRequestDescriptor]:
        owner = repository_full_name.split("/", 1)[0]
        path = self._repo_path(repository_full_name)
        status, payload = self._request(
            "GET",
            f"{path}/pulls?state=open&head={quote(f'{owner}:{head_branch}', safe='')}",
        )
        if status != 200 or not isinstance(payload, list):
            raise PullRequestError(
                PullRequestFailure.TRANSIENT, "pull request search was inconclusive"
            )
        return tuple(
            self._normalize(item, repository_full_name, project_id, milestone_id)
            for item in payload
            if isinstance(item, Mapping)
        )

    def get(
        self,
        repository_full_name: str,
        number: int,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        status, payload = self._request(
            "GET", f"{self._repo_path(repository_full_name)}/pulls/{number}"
        )
        if status != 200 or not isinstance(payload, Mapping):
            raise PullRequestError(
                PullRequestFailure.TRANSIENT, "pull request lookup was inconclusive"
            )
        return self._normalize(payload, repository_full_name, project_id, milestone_id)

    def create(
        self, repository_full_name: str, request: PullRequestCreateRequest
    ) -> PullRequestDescriptor:
        status, payload = self._request(
            "POST",
            f"{self._repo_path(repository_full_name)}/pulls",
            {
                "title": request.title,
                "body": request.body,
                "head": request.head_branch,
                "base": request.base_branch,
            },
        )
        if status != 201 or not isinstance(payload, Mapping):
            raise PullRequestError(
                PullRequestFailure.PROVIDER_REJECTION,
                "GitHub rejected pull request creation",
            )
        return self._normalize(
            payload, repository_full_name, request.project_id, request.milestone_id
        )

    @staticmethod
    def _repo_path(full_name: str) -> str:
        parts = full_name.split("/")
        if len(parts) != 2 or not all(parts):
            raise PullRequestError(
                PullRequestFailure.REPOSITORY_IDENTITY_MISMATCH,
                "repository full name is invalid",
            )
        return f"/repos/{quote(parts[0], safe='')}/{quote(parts[1], safe='')}"

    @staticmethod
    def _normalize(
        payload: Mapping[str, object],
        full_name: str,
        project_id: ProjectId,
        milestone_id: MilestoneId,
    ) -> PullRequestDescriptor:
        try:
            number, raw_state = payload["number"], payload["state"]
            head, base = payload["head"], payload["base"]
            if (
                not isinstance(number, int)
                or not isinstance(head, Mapping)
                or not isinstance(base, Mapping)
            ):
                raise ValueError
            head_repo, base_repo = head["repo"], base["repo"]
            if not isinstance(head_repo, Mapping) or not isinstance(base_repo, Mapping):
                raise ValueError
            repository_id = base_repo["id"]
            if (
                not isinstance(repository_id, int)
                or base_repo["full_name"] != full_name
                or head_repo["id"] != repository_id
            ):
                raise ValueError
            head_ref = cast(str, head["ref"])
            head_sha = cast(str, head["sha"])
            base_ref = cast(str, base["ref"])
            url = cast(str, payload["html_url"])
            if not all(
                isinstance(value, str) and value
                for value in (head_ref, head_sha, base_ref, url)
            ):
                raise ValueError
            if not url.startswith(f"https://github.com/{full_name}/pull/"):
                raise ValueError
            merged = (
                payload.get("merged") is True or payload.get("merged_at") is not None
            )
            state = (
                PullRequestState.MERGED
                if merged
                else (
                    PullRequestState.OPEN
                    if raw_state == "open"
                    else PullRequestState.CLOSED
                    if raw_state == "closed"
                    else None
                )
            )
            if state is None:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise PullRequestError(
                PullRequestFailure.TRANSIENT,
                "GitHub pull request response was malformed",
            ) from error
        merged_at = payload.get("merged_at")
        merge_sha = payload.get("merge_commit_sha")
        closed_at = payload.get("closed_at")
        return PullRequestDescriptor(
            PULL_REQUEST_INTERFACE_VERSION,
            project_id,
            milestone_id,
            repository_id,
            number,
            state,
            head_ref,
            base_ref,
            head_sha,
            url,
            merged_at if isinstance(merged_at, str) else None,
            merge_sha if isinstance(merge_sha, str) else None,
            closed_at if isinstance(closed_at, str) else None,
        )
