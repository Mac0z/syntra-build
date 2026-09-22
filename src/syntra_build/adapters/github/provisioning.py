# ruff: noqa: E501
"""Trusted GitHub REST adapter for M18 repository provisioning and verification."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from urllib.parse import quote
from urllib.request import Request

from syntra_build.adapters.github.repository_names import (
    GitHubTransport,
    _stdlib_transport,
)
from syntra_build.application.provisioning import (
    AmbiguousGitHubResult,
    ProvisioningError,
    ProvisioningFailure,
    RemoteRepository,
)
from syntra_build.domain import RepositoryVisibility
from syntra_build.infrastructure.config import ApplicationConfig

_API_ROOT = "https://api.github.com"


class GitHubProvisioningAdapter:
    """Keep authentication and provider response formats at the adapter boundary."""

    def __init__(
        self,
        config: ApplicationConfig,
        *,
        transport: GitHubTransport = _stdlib_transport,
    ) -> None:
        token = config.secrets.github_token
        if not config.github.enabled or config.github.owner is None or token is None:
            raise ProvisioningError(
                ProvisioningFailure.AUTHENTICATION,
                "GitHub provisioning is not configured",
            )
        self.owner, self._token = config.github.owner, token.value
        self._timeout, self._transport = config.github.api_timeout_seconds, transport

    def _request(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> tuple[int, object | None]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{_API_ROOT}{path}",
            data=body,
            method=method,
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
            if method != "GET":
                raise AmbiguousGitHubResult(
                    "GitHub mutation result is ambiguous"
                ) from error
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT, "GitHub request failed"
            ) from error
        try:
            decoded = json.loads(response.body) if response.body else None
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT, "GitHub response was malformed"
            ) from error
        if response.status in {401, 403}:
            raise ProvisioningError(
                ProvisioningFailure.AUTHENTICATION, "GitHub rejected the request"
            )
        if response.status >= 500:
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT, "GitHub is temporarily unavailable"
            )
        return response.status, decoded

    def get_repository(self, owner: str, name: str) -> RemoteRepository | None:
        status, payload = self._request(
            "GET", f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(payload, Mapping):
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT, "repository lookup was inconclusive"
            )
        return self._repository(payload)

    def create_repository(
        self, name: str, visibility: RepositoryVisibility
    ) -> RemoteRepository:
        status, payload = self._request(
            "POST",
            "/user/repos",
            {
                "name": name,
                "private": visibility is RepositoryVisibility.PRIVATE,
                "auto_init": False,
            },
        )
        if status == 422:
            raise ProvisioningError(
                ProvisioningFailure.COLLISION, "repository name is unavailable"
            )
        if status != 201 or not isinstance(payload, Mapping):
            raise ProvisioningError(
                ProvisioningFailure.PROVIDER_REJECTION,
                "GitHub rejected repository creation",
            )
        return self._repository(payload)

    def configure_repository(self, owner: str, name: str) -> None:
        status, _ = self._request(
            "PATCH",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}",
            {"default_branch": "main"},
        )
        if status != 200:
            raise ProvisioningError(
                ProvisioningFailure.PROVIDER_REJECTION,
                "GitHub rejected repository configuration",
            )

    def main_sha(self, owner: str, name: str) -> str | None:
        status, payload = self._request(
            "GET",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/git/ref/heads/main",
        )
        if status == 404:
            return None
        if (
            status == 409
            and isinstance(payload, Mapping)
            and payload.get("message") == "Git Repository is empty."
        ):
            return None
        if (
            status != 200
            or not isinstance(payload, Mapping)
            or not isinstance(payload.get("object"), Mapping)
        ):
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT, "main branch lookup was inconclusive"
            )
        sha = payload["object"].get("sha")
        if not isinstance(sha, str):
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT, "main branch response was malformed"
            )
        return sha

    def file_content(self, owner: str, name: str, sha: str, path: str) -> bytes | None:
        status, payload = self._request(
            "GET",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/contents/{quote(path, safe='')}?ref={quote(sha, safe='')}",
        )
        if status == 404:
            return None
        if (
            status != 200
            or not isinstance(payload, Mapping)
            or payload.get("encoding") != "base64"
            or not isinstance(payload.get("content"), str)
        ):
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT,
                "repository content response was malformed",
            )
        try:
            return base64.b64decode(payload["content"], validate=True)
        except ValueError as error:
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT, "repository content was malformed"
            ) from error

    @staticmethod
    def _repository(payload: Mapping[str, object]) -> RemoteRepository:
        try:
            raw_id = payload["id"]
            if not isinstance(raw_id, int):
                raise ValueError
            external_id = raw_id
            name, full_name = str(payload["name"]), str(payload["full_name"])
            owner_value = payload["owner"]
            if not isinstance(owner_value, Mapping):
                raise ValueError
            owner = str(owner_value["login"])
            private = payload["private"]
            if not isinstance(private, bool):
                raise ValueError
            default = payload.get("default_branch")
            if default is not None and not isinstance(default, str):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise ProvisioningError(
                ProvisioningFailure.TRANSIENT,
                "repository identity response was malformed",
            ) from error
        return RemoteRepository(
            external_id,
            owner,
            name,
            full_name,
            RepositoryVisibility.PRIVATE if private else RepositoryVisibility.PUBLIC,
            default,
        )
