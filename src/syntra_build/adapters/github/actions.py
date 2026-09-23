# ruff: noqa: E501
"""Read-only GitHub Actions discovery for an exact pull-request head SHA."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from urllib.parse import quote, urlencode
from urllib.request import Request

from syntra_build.adapters.github.repository_names import (
    GitHubTransport,
    _stdlib_transport,
)
from syntra_build.domain.ci import (
    CICheck,
    CICheckConclusion,
    CICheckStatus,
    CIObservation,
)
from syntra_build.infrastructure.config import ApplicationConfig

_API_ROOT = "https://api.github.com"


class CIProviderFailure(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    REJECTION = "REJECTION"
    TRANSIENT = "TRANSIENT"
    MALFORMED = "MALFORMED"


class CIProviderError(RuntimeError):
    def __init__(self, failure: CIProviderFailure, message: str) -> None:
        self.failure = failure
        super().__init__(message)


class GitHubActionsAdapter:
    def __init__(
        self,
        config: ApplicationConfig,
        *,
        transport: GitHubTransport = _stdlib_transport,
    ) -> None:
        token = config.secrets.github_token
        if not config.github.enabled or token is None:
            raise CIProviderError(
                CIProviderFailure.AUTHENTICATION, "GitHub CI is not configured"
            )
        self._token = token.value
        self._timeout = config.github.api_timeout_seconds
        self._transport = transport

    def _request(self, method: str, path: str) -> Mapping[str, object] | None:
        request = Request(
            f"{_API_ROOT}{path}",
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "syntra-build",
            },
        )
        try:
            response = self._transport(request, self._timeout)
        except Exception as error:
            raise CIProviderError(
                CIProviderFailure.TRANSIENT, "GitHub CI transport failed"
            ) from error
        if response.status in {401, 403}:
            raise CIProviderError(
                CIProviderFailure.AUTHENTICATION, "GitHub rejected CI access"
            )
        if response.status >= 500 or response.status == 429:
            raise CIProviderError(
                CIProviderFailure.TRANSIENT, "GitHub CI is temporarily unavailable"
            )
        expected = {200} if method == "GET" else {201}
        if response.status not in expected:
            raise CIProviderError(
                CIProviderFailure.REJECTION, "GitHub rejected CI discovery"
            )
        if not response.body and method == "POST":
            return None
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CIProviderError(
                CIProviderFailure.MALFORMED, "GitHub CI response was malformed"
            ) from error
        if not isinstance(payload, Mapping):
            raise CIProviderError(
                CIProviderFailure.MALFORMED, "GitHub CI response was unexpected"
            )
        return payload

    def _get(self, path: str) -> Mapping[str, object]:
        payload = self._request("GET", path)
        if payload is None:
            raise CIProviderError(
                CIProviderFailure.MALFORMED, "GitHub CI response was empty"
            )
        return payload

    def observe(
        self, repository_full_name: str, pull_request_number: int, head_sha: str
    ) -> CIObservation:
        repo = quote(repository_full_name, safe="/")
        runs: list[Mapping[str, object]] = []
        page = 1
        while True:
            query = urlencode(
                {
                    "head_sha": head_sha,
                    "event": "pull_request",
                    "per_page": 100,
                    "page": page,
                }
            )
            payload = self._get(f"/repos/{repo}/actions/runs?{query}")
            batch = payload.get("workflow_runs")
            if not isinstance(batch, list) or not all(
                isinstance(item, Mapping) for item in batch
            ):
                raise CIProviderError(
                    CIProviderFailure.MALFORMED, "workflow runs were malformed"
                )
            runs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        # The endpoint filter is independently checked: provider mistakes cannot
        # associate evidence from a different revision or trigger.
        applicable = []
        for run in runs:
            pull_requests = run.get("pull_requests")
            if not isinstance(pull_requests, list):
                raise CIProviderError(
                    CIProviderFailure.MALFORMED,
                    "workflow run pull-request identity was missing",
                )
            numbers = {
                item.get("number")
                for item in pull_requests
                if isinstance(item, Mapping)
            }
            if (
                run.get("head_sha") == head_sha
                and run.get("event") == "pull_request"
                and pull_request_number in numbers
            ):
                applicable.append(run)
        checks: list[CICheck] = []
        run_ids: list[str] = []
        for run in applicable:
            run_id = run.get("id")
            if not isinstance(run_id, int | str):
                raise CIProviderError(
                    CIProviderFailure.MALFORMED, "workflow run identity was missing"
                )
            run_ids.append(str(run_id))
            job_page = 1
            while True:
                jobs_payload = self._get(
                    f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={job_page}"
                )
                jobs = jobs_payload.get("jobs")
                if not isinstance(jobs, list) or not all(
                    isinstance(item, Mapping) for item in jobs
                ):
                    raise CIProviderError(
                        CIProviderFailure.MALFORMED, "workflow jobs were malformed"
                    )
                checks.extend(self._normalize_job(job) for job in jobs)
                if len(jobs) < 100:
                    break
                job_page += 1
        # Stable identity removes duplicate jobs returned during page movement.
        unique = {item.external_check_id: item for item in checks}
        return CIObservation(tuple(unique.values()), tuple(run_ids))

    def rerun(self, repository_full_name: str, workflow_run_id: str) -> None:
        """Start one trusted GitHub re-run after persisted retry intent."""
        repo = quote(repository_full_name, safe="/")
        run_id = quote(workflow_run_id, safe="")
        self._request("POST", f"/repos/{repo}/actions/runs/{run_id}/rerun")

    @staticmethod
    def _normalize_job(job: Mapping[str, object]) -> CICheck:
        external_id, name, raw_status = (
            job.get("id"),
            job.get("name"),
            job.get("status"),
        )
        if (
            not isinstance(external_id, int | str)
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise CIProviderError(
                CIProviderFailure.MALFORMED, "workflow job identity was malformed"
            )
        statuses = {
            "queued": CICheckStatus.QUEUED,
            "waiting": CICheckStatus.QUEUED,
            "requested": CICheckStatus.QUEUED,
            "pending": CICheckStatus.QUEUED,
            "in_progress": CICheckStatus.RUNNING,
            "completed": CICheckStatus.COMPLETED,
        }
        status = statuses.get(raw_status) if isinstance(raw_status, str) else None
        if status is None:
            status = CICheckStatus.COMPLETED
            conclusion = CICheckConclusion.UNKNOWN
        elif status is CICheckStatus.COMPLETED:
            conclusions = {
                "success": CICheckConclusion.PASSED,
                "failure": CICheckConclusion.FAILED,
                "timed_out": CICheckConclusion.FAILED,
                "cancelled": CICheckConclusion.CANCELLED,
                "skipped": CICheckConclusion.SKIPPED,
                "neutral": CICheckConclusion.NEUTRAL,
            }
            candidate = job.get("conclusion")
            raw_conclusion = candidate if isinstance(candidate, str) else ""
            conclusion = conclusions.get(raw_conclusion, CICheckConclusion.UNKNOWN)
        else:
            conclusion = None

        def optional_text(key: str) -> str | None:
            value = job.get(key)
            return value if isinstance(value, str) and value else None

        summary = None
        steps = job.get("steps")
        if conclusion is CICheckConclusion.FAILED and isinstance(steps, list):
            failed = [
                str(step.get("name"))
                for step in steps
                if isinstance(step, Mapping) and step.get("conclusion") == "failure"
            ]
            summary = ", ".join(failed)[:1000] or None
        return CICheck(
            name.strip(),
            str(external_id),
            status,
            conclusion,
            optional_text("started_at"),
            optional_text("completed_at"),
            optional_text("html_url"),
            summary,
        )
