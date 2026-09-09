"""Bounded, read-only GitHub repository-name conflict lookup."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from syntra_build.application.projects import RepositoryConflictCheckUnavailable
from syntra_build.infrastructure.config import ApplicationConfig

_API_ROOT = "https://api.github.com"


@dataclass(frozen=True, slots=True)
class GitHubHTTPResponse:
    status: int
    body: bytes


GitHubTransport = Callable[[Request, float], GitHubHTTPResponse]


def _stdlib_transport(request: Request, timeout: float) -> GitHubHTTPResponse:
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return GitHubHTTPResponse(response.status, response.read())
    except HTTPError as error:
        return GitHubHTTPResponse(error.code, error.read())


class GitHubRepositoryNameConflictChecker:
    """Check one exact repository identity without provider mutation methods."""

    def __init__(
        self,
        config: ApplicationConfig,
        *,
        transport: GitHubTransport = _stdlib_transport,
    ) -> None:
        token = config.secrets.github_token
        self._owner = config.github.owner
        self._token = token.value if token is not None else None
        self._configured = config.github.enabled
        self._timeout = config.github.api_timeout_seconds
        self._transport = transport

    def conflicts(self, canonical_name: str) -> bool:
        if not self._configured or self._owner is None or self._token is None:
            raise RepositoryConflictCheckUnavailable(
                "GitHub repository-name checking is not configured"
            )
        owner = quote(self._owner, safe="")
        name = quote(canonical_name, safe="")
        request = Request(
            f"{_API_ROOT}/repos/{owner}/{name}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "syntra-build",
            },
            method="GET",
        )
        try:
            response = self._transport(request, self._timeout)
        except Exception as error:
            raise RepositoryConflictCheckUnavailable(
                "GitHub repository-name check transport failed"
            ) from error
        if response.status == 404:
            return False
        if response.status != 200:
            raise RepositoryConflictCheckUnavailable(
                "GitHub repository-name check was inconclusive"
            )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RepositoryConflictCheckUnavailable(
                "GitHub repository response was malformed"
            ) from error
        if not isinstance(payload, Mapping):
            raise RepositoryConflictCheckUnavailable(
                "GitHub repository response was unexpected"
            )
        full_name = payload.get("full_name")
        expected = f"{self._owner}/{canonical_name}"
        if (
            not isinstance(full_name, str)
            or full_name.casefold() != expected.casefold()
        ):
            raise RepositoryConflictCheckUnavailable(
                "GitHub repository identity was ambiguous"
            )
        return True
