# ruff: noqa: E501
"""OpenAI Responses API Architect adapter using strict Structured Outputs."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import cast

from syntra_build.application.architect import ArchitectError, ArchitectFailureKind
from syntra_build.domain import ArchitectDesignRequest, ArchitectDesignResponse

DESIGN_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "interface_version",
        "correlation_id",
        "project_id",
        "mode",
        "message",
        "proposed_decisions",
        "open_questions",
    ],
    "properties": {
        "interface_version": {"type": "string", "const": "1.0"},
        "correlation_id": {"type": "string", "minLength": 1},
        "project_id": {"type": "string", "minLength": 1},
        "mode": {
            "type": "string",
            "enum": ["ASK_USER", "PROPOSE_DESIGN", "READY_TO_DRAFT", "BLOCKED"],
        },
        "message": {"type": "string", "minLength": 1},
        "proposed_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision_type", "title", "proposal", "rationale"],
                "properties": {
                    "decision_type": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "proposal": {"type": "string", "minLength": 1},
                    "rationale": {"type": "string", "minLength": 1},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}
Transport = Callable[[dict[str, object], str, float], dict[str, object]]


class OpenAIArchitectProvider:
    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reasoning_effort: str = "high",
        timeout_seconds: float = 120.0,
        transport: Transport | None = None,
    ) -> None:
        if not api_key or not model or timeout_seconds <= 0:
            raise ArchitectError(
                ArchitectFailureKind.CONFIGURATION,
                "OpenAI Architect configuration is incomplete",
            )
        self._api_key = api_key
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout_seconds
        self._transport = transport or self._http
        self._usage: dict[str, int | str | None] = {}

    def design(self, request: ArchitectDesignRequest) -> ArchitectDesignResponse:
        payload: dict[str, object] = {
            "model": self.model,
            "reasoning": {"effort": self._reasoning_effort},
            "instructions": "You are an advisory software architect. Do not claim authority, execute operations, or draft SPEC.md/AGENTS.md. Return only the constrained response.",
            "input": json.dumps(
                request.to_dict(), sort_keys=True, separators=(",", ":")
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "architect_design_response",
                    "strict": True,
                    "schema": DESIGN_RESPONSE_SCHEMA,
                }
            },
            "store": False,
        }
        try:
            raw = self._transport(payload, self._api_key, self._timeout)
        except ArchitectError:
            raise
        except TimeoutError as error:
            raise ArchitectError(
                ArchitectFailureKind.TIMEOUT,
                "Architect provider timed out; outcome may be ambiguous",
            ) from error
        except (OSError, urllib.error.URLError) as error:
            raise ArchitectError(
                ArchitectFailureKind.TRANSIENT_PROVIDER,
                "Architect provider is temporarily unavailable",
            ) from error
        status = raw.get("status")
        if status == "incomplete":
            raise ArchitectError(
                ArchitectFailureKind.INCOMPLETE_RESPONSE,
                "Architect provider returned an incomplete response",
            )
        output = raw.get("output")
        if not isinstance(output, list):
            raise ArchitectError(
                ArchitectFailureKind.MALFORMED_RESPONSE,
                "Architect provider returned an unusable response",
            )
        text: str | None = None
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in cast(list[object], item.get("content", [])):
                if isinstance(content, dict) and content.get("type") == "refusal":
                    raise ArchitectError(
                        ArchitectFailureKind.REFUSAL,
                        "Architect provider refused the request",
                    )
                if (
                    isinstance(content, dict)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)
                ):
                    text = cast(str, content["text"])
        if text is None:
            raise ArchitectError(
                ArchitectFailureKind.MALFORMED_RESPONSE,
                "Architect provider returned no structured output",
            )
        try:
            response = ArchitectDesignResponse.from_dict(json.loads(text))
        except (ValueError, TypeError) as error:
            raise ArchitectError(
                ArchitectFailureKind.MALFORMED_RESPONSE,
                "Architect provider returned malformed structured output",
            ) from error
        usage = raw.get("usage", {})
        usage = usage if isinstance(usage, dict) else {}
        input_details = usage.get("input_tokens_details", {})
        output_details = usage.get("output_tokens_details", {})
        response_id = raw.get("id")
        safe_response_id: str | None = (
            response_id if isinstance(response_id, str) else None
        )
        self._usage = {
            "provider_response_id": safe_response_id,
            "input_tokens": _integer(usage.get("input_tokens")),
            "cached_input_tokens": _integer(input_details.get("cached_tokens"))
            if isinstance(input_details, dict)
            else None,
            "output_tokens": _integer(usage.get("output_tokens")),
            "reasoning_tokens": _integer(output_details.get("reasoning_tokens"))
            if isinstance(output_details, dict)
            else None,
            "total_tokens": _integer(usage.get("total_tokens")),
        }
        return response

    def telemetry(self) -> dict[str, int | str | None]:
        return dict(self._usage)

    @staticmethod
    def _http(
        payload: dict[str, object], api_key: str, timeout: float
    ) -> dict[str, object]:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 429:
                kind = ArchitectFailureKind.THROTTLED
            elif error.code >= 500:
                kind = ArchitectFailureKind.TRANSIENT_PROVIDER
            elif error.code in {401, 403}:
                kind = ArchitectFailureKind.CONFIGURATION
            else:
                kind = ArchitectFailureKind.PERMANENT_PROVIDER
            raise ArchitectError(
                kind, f"Architect provider request failed (HTTP {error.code})"
            ) from error
        except TimeoutError as error:
            raise ArchitectError(
                ArchitectFailureKind.TIMEOUT,
                "Architect provider timed out; outcome may be ambiguous",
            ) from error
        if not isinstance(result, dict):
            raise ArchitectError(
                ArchitectFailureKind.MALFORMED_RESPONSE,
                "Architect provider returned an unusable envelope",
            )
        return cast(dict[str, object], result)


def _integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None
